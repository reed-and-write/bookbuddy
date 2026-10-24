"""Optional LLM cleanup pass for PDF text: pluggable providers (registry `llm_providers`), validation, per-chunk cache.

The model may only remove or repair obvious extraction debris. Anything that looks like a rewrite, a summary
or commentary is rejected and the un-LLM'd text is used for that chunk. A failing provider is skipped.

Speed/waste controls: chunks whose heuristic text already looks like clean prose are not sent at all
(`needs_cleanup_score` vs a configurable threshold), the rest go through a bounded thread pool, and an output
rejected for length/commentary is retried once with a stricter instruction before the heuristic text is kept.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import config
from .registry import llm_providers as provider_registry

log = logging.getLogger("bookbuddy.llm")

PROMPT_VERSION = "1"        # cache key only; outputs are validated, so a prompt tweak does not invalidate them
MIN_RATIO, MAX_RATIO = 0.70, 1.10

PROMPT = """You are a text-cleaning filter. The text below was automatically extracted from a PDF so it can be read aloud.
Rules, in order of importance:
1. Only REMOVE or REPAIR obvious extraction debris: stray footnote numbers and orphaned footnote text, leftover page
   headers/footers/page numbers, table or figure debris, broken words ("exam- ple"), sentences broken by a stray
   line or page break, doubled spaces, garbled symbols.
2. NEVER summarise, rewrite, paraphrase, reorder, modernise, translate or add any text. Do not fix style or grammar.
   Keep every real sentence and paragraph of the book exactly as written, in the same order. Text in [square
   brackets], quotations and commentary that belong to the book are real content: keep them.
3. Keep the blank lines between paragraphs. If nothing needs fixing, return the text unchanged.
4. Your whole reply is the cleaned text itself: it starts with the first word of the text and ends with its last word.
   No preface, no explanation, no notes, no markdown, no code fences, no <<< >>> markers.

TEXT TO CLEAN:
<<<
{text}
>>>"""

STRICT_SUFFIX = """

IMPORTANT: your previous answer was rejected ({why}). The cleaned text must be almost exactly as long as the input
(normally 90-100% of it): you may delete only isolated debris such as a lone page number, header line or footnote
number, and join broken words and lines. Do not delete or shorten any sentence or paragraph, including bracketed
notes. Reply with the cleaned text only."""

_COMMENTARY = re.compile(
    r"^\W*(here(\s+is|'s|\s+are)|sure\b|certainly\b|of course\b|okay\b|ok\b|i('ve|\s+have|\s+removed|\s+cleaned)|"
    r"below\s+is|the\s+(cleaned|corrected)\s+text|cleaned\s+text|note\s*:)", re.I)
_COMMENTARY_ANY = re.compile(r"(here is the cleaned|cleaned text:|```|<<<|>>>|as an ai\b|i (removed|cleaned|fixed) )", re.I)
_ERROR_OUT = re.compile(r"(Provider said|rejected the request|HTTP\s*4\d\d|invalidated|oauth|unauthori[sz]ed|"
                        r"not logged in|please run /login|authentication|rate limit|usage limit)", re.I)


class ProviderError(RuntimeError):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal      # True = don't try this provider again during this run


# ------------------------------------------------------------------ validation

def validate(original: str, result: str) -> str | None:
    """Return None when the LLM output is acceptable, else a reason string."""
    o, r = original.strip(), result.strip()
    if not r:
        return "empty output"
    if _COMMENTARY.match(r) or _COMMENTARY_ANY.search(r[:300] + " " + r[-300:]):
        return "contains commentary"
    ratio = len(r) / max(1, len(o))
    if ratio < MIN_RATIO:
        return f"too short ({ratio:.0%} of input)"
    if ratio > MAX_RATIO:
        return f"too long ({ratio:.0%} of input)"
    ow = set(re.findall(r"[a-z']+", o.lower()))
    rw = re.findall(r"[a-z']+", r.lower())
    if rw:
        novel = sum(1 for w in rw if w not in ow)
        if novel / len(rw) > 0.06:        # a repair may mend a word or two; a rewrite adds many
            return f"introduces new words ({novel}/{len(rw)})"
    if _moved_block(o, r):
        return "reorders text"
    return None


def _moved_block(original: str, result: str, min_words: int = 5) -> bool:
    """True when a run of >= min_words words was deleted in one place and re-inserted elsewhere (a reorder)."""
    import difflib
    ow, rw = re.findall(r"[a-z']+", original.lower()), re.findall(r"[a-z']+", result.lower())
    ops = difflib.SequenceMatcher(None, ow, rw, autojunk=False).get_opcodes()
    dels = {" ".join(ow[i1:i2]) for t, i1, i2, _, _ in ops if t in ("delete", "replace") and i2 - i1 >= min_words}
    ins = {" ".join(rw[j1:j2]) for t, _, _, j1, j2 in ops if t in ("insert", "replace") and j2 - j1 >= min_words}
    if not dels or not ins:
        return False
    return any(d in i or i in d for d in dels for i in ins)


# ------------------------------------------------------------------ "does this chunk need the LLM?"

_TERMINAL = re.compile(r"""[.!?:;'"\u2019\u201d)\]\u2026]\s*$""")
_TABLE_CHARS = re.compile(r"[|~^_#@\\{}<>=\u25a0-\u25ff\u2022\ufffd]")
_NUMW = (r"(?:\d+|[ivxlc]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|(?:thir|four|fif|six|seven|eigh|nine)teen|"
         r"(?:twen|thir|for|fif|six|seven|eigh|nine)ty(?:-(?:one|two|three|four|five|six|seven|eight|nine))?|hundred)")
# footnote/citation residue: "p. 12", "chap. forty-six", "Ibid.", 'See "..."'
_CITE = re.compile(r"\b(?:pp?|chaps?|ch|vol|bk|no|ss|sec|ff)\. ?" + _NUMW + r"\b|\b(?:op\. cit|ibid)\b|\bSee [\"\u201c]", re.I)
# a bare number word used as a whole sentence in the middle of a paragraph: a leftover footnote/verse mark
_MID_NUM = re.compile(r"(?<=[.\]\"\u201d\u2019)!?] )" + _NUMW + r"(?:-" + _NUMW + r")?\.(?= [A-Z\[\"])", re.I)


def cleanup_signals(text: str) -> dict[str, float]:
    """Counts of leftover-debris signals in heuristic-cleaned text (cheap, no model)."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    sig: dict[str, float] = {}
    # a bare number, or a number glued to a word, in the middle of prose is usually a footnote/page mark
    sig["digit_lines"] = sum(1 for l in lines if re.fullmatch(r"[\[(]?\d{1,4}[\])]?", l))
    sig["glued_marks"] = len(re.findall(r"[a-z]{3,}[.,;:]?\d{1,3}\b(?!\s*(?:st|nd|rd|th)\b)", text))
    sig["stray_numbers"] = len(re.findall(r"(?<=[a-z,;])\s\d{1,3}\s(?=[a-z])", text))
    # sentence broken by a stray line/page break: paragraph without terminal punctuation followed by lower-case start
    broken = 0
    for a, b in zip(paras, paras[1:]):
        if not _TERMINAL.search(a) and b[:1].islower():
            broken += 1
    sig["broken_paras"] = broken
    sig["short_fragments"] = sum(1 for p in paras if len(p) < 40 and not _TERMINAL.search(p) and len(paras) > 1)
    sig["hyphen_breaks"] = len(re.findall(r"[a-z]- [a-z]|[a-z]-\n[a-z]", text))
    sig["table_chars"] = len(_TABLE_CHARS.findall(text))
    sig["cite_refs"] = len(_CITE.findall(text))
    sig["mid_numbers"] = len(_MID_NUM.findall(text))
    sig["double_spaces"] = len(re.findall(r"\S {2,}\S", text))
    # repeated header remnants: the same short line recurring inside one chunk
    norm = [re.sub(r"\d+", "#", re.sub(r"\s+", " ", l.lower())) for l in lines if len(l) < 70]
    seen: dict[str, int] = {}
    for l in norm:
        seen[l] = seen.get(l, 0) + 1
    sig["repeated_lines"] = sum(c for l, c in seen.items() if c > 1 and len(l) > 3)
    # table-like runs: many short lines in a row, tab/space columns, or very digit-heavy text
    sig["column_gaps"] = len(re.findall(r"\S {3,}\S|\t", text))
    run = best = 0
    for p in paras:
        run = run + 1 if len(p) < 60 else 0
        best = max(best, run)
    sig["short_run"] = best if best >= 4 else 0
    sig["digit_heavy"] = 1.0 if len(re.findall(r"\d", text)) > 0.08 * max(1, len(text)) else 0.0
    return sig


_WEIGHTS = {"digit_lines": 3.0, "glued_marks": 1.0, "stray_numbers": 1.0, "broken_paras": 2.0, "short_fragments": 1.0,
            "hyphen_breaks": 1.0, "table_chars": 0.5, "cite_refs": 0.25, "mid_numbers": 0.5, "double_spaces": 0.5, "repeated_lines": 1.5, "column_gaps": 1.0,
            "short_run": 1.0, "digit_heavy": 3.0}


def needs_cleanup_score(text: str) -> float:
    """Weighted debris signals per ~4500-char chunk; 0 for clean prose. Compare with the llm.min_score setting
    (1.0 = about one real debris signal per chunk)."""
    sig = cleanup_signals(text)
    raw = sum(_WEIGHTS[k] * v for k, v in sig.items())
    return raw * 4500.0 / max(1500, len(text))


# ------------------------------------------------------------------ providers

def looks_like_error(out: str) -> bool:
    """True when a provider's stdout looks like an auth/rate-limit error message rather than cleaned text.
    Helper for provider plugins."""
    return bool(_ERROR_OUT.search(out[:600])) and len(out) < 800


class Provider:
    """Interface for an LLM cleanup provider (register it in the `bookbuddy.llm_providers` entry-point group).

    `run(prompt)` returns the model's reply text or raises ProviderError (fatal=True = stop using it for this run).
    It is constructed as `Provider(**settings)` with the keys of the `[llm.<name>]` table in config.toml.
    `est_seconds_per_chunk` only feeds the up-front time estimate."""
    name = "none"
    est_seconds_per_chunk = 15.0

    def __init__(self, **settings):
        self.settings = settings

    def run(self, prompt: str) -> str:            # pragma: no cover - interface
        raise NotImplementedError


def _configured(name: str) -> bool:
    """Keyed providers (anthropic, openai) count only when the user supplied a key; plugin providers always do."""
    fn = getattr(provider_registry.get(name), "is_configured", None)
    return True if fn is None else bool(fn())


def resolve_order(choice: str | None = None, order: list[str] | None = None) -> list[str]:
    """Provider names for a choice: 'none' -> [], 'auto' -> the configured order (only installed providers, and only
    when `[llm] enabled` is true), else [name] (naming a provider explicitly is itself the opt-in)."""
    from . import llm_providers  # noqa: F401  (registers the built-in anthropic/openai providers)
    choice = (choice or config.get_settings().llm_provider or "auto").lower()
    if choice in ("none", "off", "local"):
        return []
    if choice == "auto":
        if not config.get_settings().llm_enabled:
            return []
        order = config.get_settings().llm_order if order is None else order
        return [n for n in order if provider_registry.has(n) and _configured(n)]
    if provider_registry.has(choice):
        if not _configured(choice):
            raise ValueError(f"LLM provider {choice!r} has no API key (set it in the web UI, config.toml or the environment)")
        return [choice]
    raise ValueError(f"unknown LLM provider {choice!r} (installed: {', '.join(provider_registry.names()) or 'none'}; "
                     "or use 'none')")


def make_providers(names: list[str]) -> list[Provider]:
    st = config.get_settings()
    out = []
    for n in names:
        kw = dict(st.llm_providers.get(n, {}))
        kw.setdefault("timeout", st.llm_timeout)
        out.append(provider_registry.get(n)(**kw))
    return out


# ------------------------------------------------------------------ cleaner

_RETRYABLE = ("too short", "too long", "contains commentary", "empty output")


class Cleaner:
    """Cleans text chunk by chunk. `providers` is an ordered list of Provider objects (empty = no LLM).

    workers   : bounded thread pool size for chunk calls (output order is always the input order)
    min_score : chunks scoring below this on needs_cleanup_score() are not sent (0 = send everything)
    on_event  : optional (kind, **info) hook; emits 'llm_plan' once and 'llm_progress' every `progress_every` chunks
    """

    def __init__(self, providers: list[Provider], cache_dir: Path | None = None, workers: int | None = None,
                 min_score: float | None = None, progress_every: int | None = None, on_event: Callable | None = None):
        self.providers = list(providers)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else config.get_settings().cache_dir / "llm"
        self.workers = max(1, int(config.get_settings().llm_workers if workers is None else workers))
        self.min_score = config.get_settings().llm_min_score if min_score is None else min_score
        self.progress_every = max(1, int(config.get_settings().llm_progress_every if progress_every is None else progress_every))
        self.on_event = on_event
        self.dead: set[str] = set()
        self.used: dict[str, int] = {}          # provider name -> chunks handled (incl. cache hits)
        self.stats = {"chunks": 0, "cached": 0, "skipped": 0, "sent": 0, "rejected": 0, "failed": 0,
                      "retried": 0, "retry_ok": 0}
        self._soft_fails: dict[str, int] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_choice(cls, choice: str, cache_dir: Path | None = None, **kw) -> "Cleaner":
        return cls(make_providers(resolve_order(choice)), cache_dir, **kw)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] += n

    def _emit(self, kind: str, **info) -> None:
        if self.on_event:
            try:
                self.on_event(kind, **info)
            except Exception:  # noqa: BLE001 - notifications must never fail a job
                log.exception("LLM progress hook failed")

    # -- cache (thread-safe: unique tmp name per write, atomic replace, lock around the write)
    def _key(self, text: str) -> str:
        return hashlib.sha256(f"v{PROMPT_VERSION}|{text}".encode()).hexdigest()[:32]

    def _cache_get(self, key: str) -> tuple[str, str] | None:
        try:
            d = json.loads((self.cache_dir / f"{key}.json").read_text(encoding="utf-8"))
            return d["provider"], d["text"]
        except (OSError, ValueError, KeyError):
            return None

    def _cache_put(self, key: str, provider: str, text: str) -> None:
        try:
            with self._lock:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.cache_dir / f"{key}.{threading.get_ident()}.tmp"
                tmp.write_text(json.dumps({"provider": provider, "text": text}), encoding="utf-8")
                tmp.replace(self.cache_dir / f"{key}.json")
        except OSError as e:
            log.warning("could not write LLM cache: %s", e)

    def _count_used(self, name: str) -> None:
        with self._lock:
            self.used[name] = self.used.get(name, 0) + 1

    # -- one chunk
    def _call(self, p: Provider, text: str, why: str | None = None) -> tuple[str | None, str | None]:
        """One provider call + validation. Returns (clean_text, None) or (None, reason). ProviderError propagates."""
        prompt = PROMPT.format(text=text) + (STRICT_SUFFIX.format(why=why) if why else "")
        out = p.run(prompt)
        reason = validate(text, out)
        return (None, reason) if reason else (out.strip(), None)

    def _retry_allowed(self) -> bool:
        with self._lock:     # retry storm guard: several retries and none ever helped => stop retrying this run
            return not (self.stats["retried"] >= 6 and self.stats["retry_ok"] == 0)

    def _send(self, text: str, key: str) -> str:
        """Send one chunk to the first live provider; never raises, falls back to `text`."""
        for p in self.providers:
            with self._lock:
                if p.name in self.dead:
                    continue
            try:
                res, why = self._call(p, text)
                if why and why.startswith(_RETRYABLE) and self._retry_allowed():
                    self._bump("retried")
                    log.info("LLM (%s) output rejected (%s); retrying once with the strict instruction", p.name, why)
                    res, why2 = self._call(p, text, why)
                    if res is not None:
                        self._bump("retry_ok")
                    why = why2
            except ProviderError as e:
                self._bump("failed")
                with self._lock:
                    n = self._soft_fails[p.name] = self._soft_fails.get(p.name, 0) + 1
                    if e.fatal or n >= 3:
                        self.dead.add(p.name)
                log.warning("LLM provider %s failed: %s%s", p.name, e, " (skipping it for this run)" if e.fatal else "")
                continue
            except Exception as e:  # noqa: BLE001 - the cleanup pass must never fail the job
                self._bump("failed")
                with self._lock:
                    self.dead.add(p.name)
                log.warning("LLM provider %s crashed: %r (skipping it for this run)", p.name, e)
                continue
            if res is None:
                self._bump("rejected")
                log.warning("LLM (%s) output rejected (%s); keeping heuristic text for this chunk", p.name, why)
                return text
            self._count_used(p.name)
            self._cache_put(key, p.name, res)
            return res
        return text

    def clean_chunk(self, text: str, force: bool = False) -> str:
        """Clean one chunk (cache -> skip-if-clean -> LLM). `force` ignores the needs-cleanup threshold."""
        self._bump("chunks")
        if not self.providers or not text.strip():
            return text
        key = self._key(text)
        hit = self._cache_get(key)
        if hit:
            self._bump("cached")
            self._count_used(hit[0])
            return hit[1]
        if not force and self.min_score > 0 and needs_cleanup_score(text) < self.min_score:
            self._bump("skipped")
            return text
        self._bump("sent")
        return self._send(text, key)

    # -- many paragraphs / chapters
    @staticmethod
    def group(paras: list[str], limit: int) -> list[list[str]]:
        groups: list[list[str]] = []
        cur: list[str] = []
        size = 0
        for p in paras:
            if cur and size + len(p) > limit:
                groups.append(cur)
                cur, size = [], 0
            cur.append(p)
            size += len(p) + 2
        if cur:
            groups.append(cur)
        return groups

    def clean_chapters(self, chapters: list[list[str]], chunk_chars: int | None = None) -> list[list[str]]:
        """Clean every chapter's paragraphs through one pool. Output order/shape is deterministic."""
        if not self.providers:
            return chapters
        limit = chunk_chars or config.get_settings().llm_chunk_chars
        grouped = [self.group(paras, limit) for paras in chapters]
        texts = [(ci, gi, "\n\n".join(g)) for ci, gs in enumerate(grouped) for gi, g in enumerate(gs)]

        # decide up front what is cached / skipped / sent, so the plan and ETA are known before any call
        results: dict[tuple[int, int], str] = {}
        todo: list[tuple[int, int, str]] = []
        for ci, gi, t in texts:
            self._bump("chunks")
            if not t.strip():
                results[(ci, gi)] = t
                continue
            hit = self._cache_get(self._key(t))
            if hit:
                self._bump("cached")
                self._count_used(hit[0])
                results[(ci, gi)] = hit[1]
            elif self.min_score > 0 and needs_cleanup_score(t) < self.min_score:
                self._bump("skipped")
                results[(ci, gi)] = t
            else:
                todo.append((ci, gi, t))
        n_send = len(todo)
        per = getattr(self.providers[0], 'est_seconds_per_chunk', 15.0)
        est = -(-n_send // self.workers) * per if n_send else 0.0
        log.info("LLM cleanup plan: %d chunks: %d cached, %d skipped (clean enough, score < %.2f), %d to send "
                 "with %d workers via %s (~%.0f s)", len(texts), self.stats["cached"], self.stats["skipped"],
                 self.min_score, n_send, self.workers, self.providers[0].name, est)
        self._emit("llm_plan", chunks=len(texts), cached=self.stats["cached"], skipped=self.stats["skipped"],
                   to_send=n_send, workers=self.workers, provider=self.providers[0].name, est_seconds=est)

        if todo:
            t0 = time.time()
            done = 0
            with ThreadPoolExecutor(max_workers=min(self.workers, n_send), thread_name_prefix="llm") as ex:
                futs = {ex.submit(self._send_counted, t): (ci, gi, t) for ci, gi, t in todo}
                for f in as_completed(futs):
                    ci, gi, t = futs[f]
                    try:
                        results[(ci, gi)] = f.result()
                    except Exception:  # noqa: BLE001
                        log.exception("LLM chunk crashed; keeping heuristic text")
                        results[(ci, gi)] = t
                    done += 1
                    if done % self.progress_every == 0 or done == n_send:
                        el = time.time() - t0
                        eta = el / done * (n_send - done)
                        log.info("LLM cleanup %d/%d chunks sent (%d rejected, %d failed), %.0f s elapsed, ETA %.0f s",
                                 done, n_send, self.stats["rejected"], self.stats["failed"], el, eta)
                        self._emit("llm_progress", done=done, total=n_send, eta_seconds=eta, elapsed=el)
            log.info("LLM cleanup finished in %.0f s: stats %s", time.time() - t0, self.stats)

        out: list[list[str]] = []
        for ci, gs in enumerate(grouped):
            paras: list[str] = []
            for gi, g in enumerate(gs):
                res = results.get((ci, gi), "\n\n".join(g))
                if res == "\n\n".join(g):          # untouched (skipped, rejected, failed): keep paragraphs as they were
                    paras.extend(g)
                else:
                    paras.extend(x.strip() for x in re.split(r"\n\s*\n", res) if x.strip())
            out.append(paras)
        return out

    def _send_counted(self, text: str) -> str:
        self._bump("sent")
        return self._send(text, self._key(text))

    def clean_paragraphs(self, paras: list[str], chunk_chars: int | None = None) -> list[str]:
        return self.clean_chapters([paras], chunk_chars)[0]

    @property
    def provider_label(self) -> str:
        if not self.used:
            return "none"
        return "+".join(sorted(self.used, key=lambda n: -self.used[n]))

    def summary(self) -> str:
        s = self.stats
        return (f"{s['chunks']} chunks: {s['cached']} cached, {s['skipped']} skipped, {s['sent']} sent, "
                f"{s['rejected']} rejected, {s['failed']} provider failures, {s['retry_ok']}/{s['retried']} retries ok")

