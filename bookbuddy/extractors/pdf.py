"""PDF -> Book(title, author, cover, chapters). pdftotext (poppler) + heuristic cleanup + optional LLM pass.

Same output structure as epub_extract.extract_epub, so chunking/TTS/assembly are untouched. OCR is not part of this
phase: image-only pages are OCR'd (bookbuddy.ocr) before `build_book`; ScannedPdfError is raised only when OCR cannot run.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..book import Book, Chapter
from ..llm_clean import Cleaner
from ..textnorm import display_title, normalize_text
from . import drm
from .epub import FRONT_BACK_BODY, FRONT_BACK_TITLE

log = logging.getLogger("bookbuddy.pdf")

MIN_PAGE_CHARS = 25              # a page with fewer non-space characters than this is treated as an image
MIN_CHARS_PER_PAGE = 30          # average non-space characters per page below this = scanned / image-only


class ScannedPdfError(RuntimeError):
    pass


# ------------------------------------------------------------------ poppler wrappers

def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def pdf_info(path: Path) -> dict[str, str]:
    r = _run(["pdfinfo", "-enc", "UTF-8", str(path)])
    if r.returncode != 0:
        drm.check_pdf_error(r.stderr.decode("utf-8", "replace"))
        raise ValueError(f"pdfinfo failed (is this a valid PDF?): {r.stderr.decode('utf-8', 'replace')[-300:]}")
    info: dict[str, str] = {}
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
    return info


_PAGE_RE = re.compile(r"<page\b[^>]*>(.*?)</page>", re.S)
_BLOCK_RE = re.compile(r"<block\b[^>]*>(.*?)</block>", re.S)
_LINE_RE = re.compile(r"<line\b([^>]*)>(.*?)</line>", re.S)
_WORD_RE = re.compile(r"<word\b([^>]*)>(.*?)</word>", re.S)


def _attrs(s: str) -> dict[str, float]:
    return {k: float(v) for k, v in re.findall(r'(xMin|yMin|xMax|yMax)="([-\d.]+)"', s)}


def parse_bbox_layout(xml: str) -> list[str]:
    """pdftotext -bbox-layout output -> page texts. Poppler's own block/flow analysis gives the reading order
    (columns come out one after the other); a blank line is emitted before every block, before every indented
    line (paragraph start) and after a vertical gap, so paragraph structure survives."""
    import html
    pages: list[str] = []
    for pm in _PAGE_RE.finditer(xml):
        out: list[str] = []
        for bm in _BLOCK_RE.finditer(pm.group(1)):
            lines = []
            for lm in _LINE_RE.finditer(bm.group(1)):
                la = _attrs(lm.group(1))
                words = [(_attrs(a), html.unescape(w)) for a, w in _WORD_RE.findall(lm.group(2))]
                if not words:
                    continue
                txt = words[0][1]
                for (pa, _), (wa, wt) in zip(words, words[1:]):
                    h = max(1.0, la.get("yMax", 10) - la.get("yMin", 0))
                    txt += ("" if wa["xMin"] - pa["xMax"] < 0.12 * h else " ") + wt
                lines.append((la, txt))
            if not lines:
                continue
            left = min(la["xMin"] for la, _ in lines)
            prev = None
            out.append("")
            for la, txt in lines:
                h = max(1.0, la["yMax"] - la["yMin"])
                if prev is not None:
                    gap = la["yMin"] - prev["yMax"]
                    if gap > 0.9 * h or la["xMin"] - left > 0.9 * h:
                        out.append("")
                out.append(txt)
                prev = la
        pages.append("\n".join(out).strip("\n"))
    return pages


def pdf_pages(path: Path) -> list[str]:
    """Text of each page, in poppler's reading order, with blank lines between paragraphs/blocks."""
    r = _run(["pdftotext", "-enc", "UTF-8", "-bbox-layout", str(path), "-"], timeout=900)
    if r.returncode != 0:
        drm.check_pdf_error(r.stderr.decode("utf-8", "replace"))
    if r.returncode == 0:
        try:
            pages = parse_bbox_layout(r.stdout.decode("utf-8", "replace"))
            if pages:
                return pages
        except Exception as e:  # noqa: BLE001
            log.warning("bbox-layout parse failed (%s); using plain pdftotext", e)
    r = _run(["pdftotext", "-enc", "UTF-8", str(path), "-"], timeout=900)
    if r.returncode != 0:
        drm.check_pdf_error(r.stderr.decode("utf-8", "replace"))
        raise ValueError(f"pdftotext failed: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    pages = r.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def pdf_cover(path: Path) -> bytes | None:
    """Page 1 rendered as a JPEG (the fallback cover)."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "cover"
        r = _run(["pdftoppm", "-jpeg", "-r", "80", "-f", "1", "-l", "1", "-singlefile", str(path), str(base)])
        f = base.with_suffix(".jpg")
        return f.read_bytes() if r.returncode == 0 and f.exists() else None


def pdf_outline(path: Path) -> list[tuple[int, str, int]]:
    """Bookmarks as [(level, title, 0-based page)] in document order. Empty when absent/unreadable."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        out: list[tuple[int, str, int]] = []

        def walk(items, level):
            for it in items:
                if isinstance(it, list):
                    walk(it, level + 1)
                    continue
                try:
                    out.append((level, str(it.title).strip(), reader.get_destination_page_number(it)))
                except Exception:  # noqa: BLE001
                    continue
        walk(reader.outline, 0)
        return [o for o in out if o[1] and o[2] is not None and o[2] >= 0]
    except Exception as e:  # noqa: BLE001
        log.debug("no usable outline: %s", e)
        return []


# ------------------------------------------------------------------ patterns

_NUMWORDS = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
             r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty")
CHAPTER_RE = re.compile(
    rf"^\s*(?P<kw>chapter|chap\.|part|book|section|canto|act|letter)\s+(?P<num>\d{{1,3}}|[ivxlcdm]{{1,8}}|{_NUMWORDS})\b"
    rf"\s*(?P<rest>[.:\-—–]?\s*.*)$", re.I)
NAMED_RE = re.compile(r"^\s*(prologue|epilogue|preface|foreword|introduction|prelude|afterword|interlude|author'?s note)\s*\.?\s*$", re.I)
MATTER_RE = re.compile(
    r"^\s*(table of contents|contents|copyright( page)?|dedication|acknowledg(e)?ments?|also by .{0,40}|other books .{0,40}|"
    r"about the authors?|praise for .{0,60}|index|bibliography|references|endnotes|notes|footnotes|glossary|"
    r"colophon|list of (illustrations|figures|tables)|title page|half[- ]title)\s*$", re.I)
NUMBERED_RE = re.compile(r"^\s*(\d{1,2})\.?\s+([A-Z][^\n]{2,60})$")
PAGE_NUM_RE = re.compile(r"^\s*(?:page\s+)?[-–—\[(]?\s*(\d{1,4}|[ivxlc]{1,6})\s*[-–—\])]?(?:\s+of\s+\d+)?\s*$", re.I)
TOC_LINE_RE = re.compile(r"(\.{3,}|(\.\s){3,}|\s{3,})\s*[\divxlc]{1,5}\s*$|\.\s*\d{1,4}\s*$", re.I)
INDEX_LINE_RE = re.compile(r".{2,}?,\s*\d{1,4}([,–-]\s*\d{1,4})*\s*$")
SENT_END = re.compile(r"""[.!?]["'”’)\]]*$""")


_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with", "from", "nor"}


def smart_title(t: str) -> str:
    words = t.lower().split()
    out = [w if (0 < i < len(words) - 1 and w in _SMALL and not words[i - 1].endswith((".", ":"))) else w[:1].upper() + w[1:]
           for i, w in enumerate(words)]
    return re.sub(r"(\w)'S\b", r"\1's", " ".join(out))


def _is_chapterish(line: str) -> bool:
    s = line.strip()
    return bool(CHAPTER_RE.match(s) or NAMED_RE.match(s)) and len(s) <= 90


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ------------------------------------------------------------------ page-level cleanup

def _norm_header(line: str) -> str:
    s = re.sub(r"\s+", " ", line.strip().lower())
    # a leading or trailing number is a page number: "12 | the title" == "13 | the title"
    s = re.sub(r"^\d{1,4}\b|\b\d{1,4}$", "#", s)
    return s


def remove_headers_footers(pages: list[list[str]]) -> tuple[list[list[str]], int]:
    """Drop lines repeated near the top/bottom of many pages, and standalone page numbers at page edges."""
    n = len(pages)
    edge = 3
    counts: dict[str, int] = {}
    cand: list[list[tuple[int, str]]] = []
    for lines in pages:
        idx = [i for i, l in enumerate(lines) if l.strip()]
        sel = idx[:edge] + idx[-edge:] if len(idx) > edge else idx
        keys = []
        for i in dict.fromkeys(sel):
            k = _norm_header(lines[i])
            if not k or _is_chapterish(lines[i]):
                continue
            keys.append((i, k))
        cand.append(keys)
        for k in {k for _, k in keys}:
            counts[k] = counts.get(k, 0) + 1
    need = max(3, int(n * 0.25 + 0.5)) if n >= 6 else max(2, n // 2 + 1) if n >= 3 else 99
    repeated = {k for k, c in counts.items() if c >= need}
    out, removed = [], 0
    for lines, keys in zip(pages, cand):
        drop = {i for i, k in keys if k in repeated}
        idx = [i for i, l in enumerate(lines) if l.strip()]
        for i in idx[:2] + idx[-2:]:                     # standalone page numbers at the edges
            if PAGE_NUM_RE.match(lines[i]) and not _is_chapterish(lines[i]):
                drop.add(i)
        removed += len(drop)
        out.append([l for i, l in enumerate(lines) if i not in drop])
    return out, removed


_FN_START = re.compile(r"^\s*(\d{1,2}|[*†‡§]+)[.)]?\s+(?=\S)")


def remove_footnotes(lines: list[str]) -> list[str]:
    """Drop a block of footnotes at the bottom of a page. A footnote is recognised by a numbered/symbol first line
    in the last few lines AND the marker also appearing inline earlier on the page (or a rule/blank separator)."""
    nz = [i for i, l in enumerate(lines) if l.strip()]
    if len(nz) < 4:
        return lines
    tail_start = nz[max(0, len(nz) - 8)]
    start = None
    for i in range(len(lines) - 1, tail_start - 1, -1):
        m = _FN_START.match(lines[i]) if lines[i].strip() else None
        if not m:
            continue
        marker = m.group(1)
        body = "\n".join(lines[:i])
        if marker.isdigit():
            n = int(marker)
            inline = re.search(rf"[A-Za-z.,;:!?\"”’'\)]\s?{n}(?=[\s.,;:!?]|$)", body) is not None
            blank_before = i > 0 and not lines[i - 1].strip()
            ok = n <= 40 and (inline or (blank_before and len(nz) - nz.index(i) <= 5 and lines[i][m.end():m.end() + 1].isupper()))
        else:
            ok = marker[0] in body
        if ok:
            start = i
    if start is None:
        return lines
    keep = lines[:start]
    # strip the inline reference marks of the removed footnotes
    markers = {m.group(1) for l in lines[start:] if (m := _FN_START.match(l))}
    text = "\n".join(keep)
    for mk in markers:
        if mk.isdigit():
            text = re.sub(rf"(?<=[A-Za-z.,;:!?\"”’'\)])\s?{mk}(?=[\s.,;:!?]|$)", "", text)
        else:
            text = text.replace(mk, "")
    return text.split("\n")


def is_toc_page(lines: list[str]) -> bool:
    body = [l for l in lines if l.strip()]
    if len(body) < 4:
        return False
    hits = sum(1 for l in body if TOC_LINE_RE.search(l.strip()))
    head = " ".join(body[:3]).lower()
    return hits / len(body) >= 0.5 or (hits >= 3 and re.search(r"\bcontents\b", head) is not None)


def is_index_page(lines: list[str]) -> bool:
    body = [l for l in lines if l.strip()]
    return len(body) >= 8 and sum(1 for l in body if INDEX_LINE_RE.match(l.strip())) / len(body) >= 0.5


def is_copyright_page(lines: list[str]) -> bool:
    t = " ".join(lines)
    return len(t) < 3500 and FRONT_BACK_BODY.search(t) is not None


# ------------------------------------------------------------------ headings and sections

@dataclass
class Heading:
    line: int           # index into the flat line list where the heading starts
    skip: int           # number of lines the heading occupies
    title: str
    matter: bool = False   # front/back matter heading: its section is dropped


def detect_headings(flat: list[tuple[int, str]]) -> list[Heading]:
    """Heading patterns over the flat (page, line) list."""
    lines = [l.strip() for _, l in flat]
    n = len(lines)
    found: list[Heading] = []

    def blank_before(i):
        return i == 0 or not lines[i - 1]

    def subtitle(i):
        """Optional title line right after 'CHAPTER I': short, no final period, not another heading."""
        j = i + 1
        if j < n and not lines[j] and j + 1 < n:      # layout extraction puts a blank line between the two
            j += 1
        if j < n and lines[j] and len(lines[j]) <= 70 and not SENT_END.search(lines[j]) and not _is_chapterish(lines[j]) \
                and not CHAPTER_RE.match(lines[j]):
            return lines[j]
        return ""

    for i, l in enumerate(lines):
        if not l or len(l) > 90:
            continue
        if TOC_LINE_RE.search(l):
            continue
        m = CHAPTER_RE.match(l)
        if m and not l.endswith(","):
            rest = m.group("rest").strip(" .:-—–")
            # "Part of the reason..." style sentences: a keyword followed by a lowercase word that is not a number
            title = f"{m.group('kw').title().rstrip('.')} {m.group('num')}"
            skip = 1
            if rest:
                if len(rest) > 70 or SENT_END.search(rest) and len(rest) > 40:
                    continue
                title += f". {rest}"
            else:
                sub = subtitle(i)
                if sub:
                    title += f". {sub}"
                    skip = 2 + (1 if lines[i + 1] == '' else 0)
            found.append(Heading(i, skip, display_title(re.sub(r"\. (.*)$", lambda m: ". " + (smart_title(m.group(1)) if m.group(1).isupper() else m.group(1)), title))))
            continue
        if NAMED_RE.match(l):
            found.append(Heading(i, 1, display_title(l.strip(" .").title())))
            continue
        if MATTER_RE.match(l):
            found.append(Heading(i, 1, l.strip(), matter=True))

    # numbered / all-caps headings only when there is no stronger signal
    strong = [h for h in found if not h.matter]
    if len(strong) < 2:
        caps: list[Heading] = []
        nums: list[Heading] = []
        for i, l in enumerate(lines):
            if not l or not blank_before(i) or (i + 1 < n and lines[i + 1]):
                continue
            letters = [c for c in l if c.isalpha()]
            if (3 <= len(l) <= 50 and len(letters) >= 3 and l == l.upper() and not SENT_END.search(l)
                    and not MATTER_RE.match(l) and not PAGE_NUM_RE.match(l) and not TOC_LINE_RE.search(l)):
                caps.append(Heading(i, 1, display_title(l)))
            m = NUMBERED_RE.match(l)
            if m and not SENT_END.search(l) and not TOC_LINE_RE.search(l):
                nums.append(Heading(i, 1, display_title(f"{m.group(1)}. {m.group(2)}")))
        matter = [h for h in found if h.matter]
        # numbered headings must run 1, 2, 3...
        seq, want = [], 1
        for h in nums:
            if int(re.match(r"\d+", h.title).group()) == want:
                seq.append(h)
                want += 1
        pick = seq if len(seq) >= 3 else caps if len(caps) >= 3 else []
        # drop all-caps "headings" that are just repeats of the same words (leftover running heads)
        if pick:
            titles = [_key(h.title) for h in pick]
            if len(set(titles)) < max(2, len(titles) // 2):
                pick = []
        found = sorted(strong + pick + matter, key=lambda h: h.line)
    else:
        found.sort(key=lambda h: h.line)

    # collapse a running head that repeats the previous chapter heading verbatim
    out: list[Heading] = []
    for h in found:
        if out and not h.matter and _key(out[-1].title) == _key(h.title):
            continue
        out.append(h)
    return out


def headings_from_outline(outline: list[tuple[int, str, int]], flat: list[tuple[int, str]],
                          page_first_line: dict[int, int]) -> list[Heading]:
    """Turn PDF bookmarks into headings. Uses the shallowest level that has >= 2 usable entries."""
    if not outline:
        return []
    levels = sorted({lv for lv, _, _ in outline})
    chosen: list[tuple[int, str, int]] = []
    for lv in levels:
        cand = [o for o in outline if o[0] == lv]
        if len(cand) >= 2:
            chosen = cand
            break
    if len(chosen) < 2:
        return []
    out: list[Heading] = []
    cursor = 0
    for _, title, page in sorted(chosen, key=lambda o: o[2]):
        start = page_first_line.get(page)
        if start is None:                      # page was dropped (front matter) or has no text: next page that has text
            nxt = [p for p in page_first_line if p >= page]
            if not nxt:
                continue
            start = page_first_line[min(nxt)]
        start = max(start, cursor)
        tk = _key(title)
        skip = 0
        end_search = min(len(flat), start + 40)
        for i in range(start, end_search):
            lk = _key(flat[i][1])
            if len(lk) >= 3 and (lk == tk or (tk.startswith(lk) and len(lk) >= 6) or (lk.startswith(tk) and len(tk) >= 4)):
                if flat[i][0] > page and i > start:
                    break
                start, skip = i, 1
                # the heading may wrap over several lines
                while start + skip < len(flat) and len(_key(flat[start + skip][1])) >= 2 and tk.startswith(_key(" ".join(flat[j][1] for j in range(start, start + skip + 1)))) \
                        and len(_key(" ".join(flat[j][1] for j in range(start, start + skip + 1)))) <= len(tk):
                    skip += 1
                break
        cursor = start + skip
        out.append(Heading(start, skip, display_title(smart_title(title) if title.isupper() else title), matter=bool(FRONT_BACK_TITLE.match(title))))
    return out


# ------------------------------------------------------------------ paragraphs

def dehyphenate(text: str) -> str:
    text = text.replace("­", "")
    return re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", text)


def rebuild_paragraphs(flat_lines: list[tuple[int, str]]) -> list[str]:
    """Rejoin hard-wrapped lines into paragraphs, de-hyphenating line-break hyphens."""
    body = [l.strip() for _, l in flat_lines if l.strip()]
    if not body:
        return []
    lens = sorted(len(l) for l in body)
    full = lens[int(len(lens) * 0.9)] or 1
    paras: list[list[str]] = []
    cur: list[str] = []
    prev_page = None
    prev = None
    for page, raw in flat_lines:
        l = raw.strip()
        if not l:
            if cur and (prev_page == page):     # blank line within a page = paragraph gap
                paras.append(cur)
                cur = []
            prev = None if not cur else prev
            continue
        if cur:
            p = cur[-1]
            short = len(p) < 0.78 * full
            ends = bool(SENT_END.search(p))
            if ends and short and not re.search(r"[a-z]-$", p):
                paras.append(cur)
                cur = []
        cur.append(l)
        prev_page = page
        prev = l
    if cur:
        paras.append(cur)
    out: list[str] = []
    for p in paras:
        t = dehyphenate("\n".join(p))
        t = re.sub(r"\s*\n\s*", " ", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        if not t:
            continue
        # a column/page break in the middle of a sentence: previous paragraph never ended, this one starts lowercase
        if out and not SENT_END.search(out[-1]) and t[0].islower() and len(out[-1]) > 20:
            out[-1] = f"{out[-1]} {t}"
        else:
            out.append(t)
    return out


# ------------------------------------------------------------------ metadata

_JUNK_TITLE = re.compile(r"^(untitled|microsoft word|document\d*|slide\s*\d+|.*\.(docx?|pdf|tex|indd|qxd|pages)$)", re.I)


def pretty_stem(path: Path) -> str:
    s = re.sub(r"[_]+", " ", path.stem)
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if s.islower() or s.isupper() else s


_JUNK_AUTHOR = re.compile(r"^(unknown|anonymous|admin(istrator)?|user|owner|author|none|n/?a|null|microsoft.*|pdf.*|"
                          r"adobe.*|\W*|.*\.(docx?|pdf|tex|indd|qxd|pages))$", re.I)
_NAME_STOP = {"the", "of", "and", "a", "an", "in", "on", "to", "for", "with", "from", "novel", "volume", "vol", "edition",
              "book", "part", "chapter", "press", "publishers", "publishing", "university", "library", "project",
              "gutenberg", "ebook", "copyright", "translated", "translation", "edited", "introduction", "contents",
              "by", "is", "are", "was", "this", "that", "all", "rights", "reserved", "page", "isbn", "www", "http"}
_ROLE_BEFORE = re.compile(r"(translated|translation|edited|illustrated|introduction|foreword|preface|photographs?|"
                          r"compiled|retold|adapted|narrated|read|published|prepared|notes|commentary)\W*$", re.I)


def looks_like_name(s: str, single_ok: bool = False) -> bool:
    """2-5 capitalised word tokens (1 when `single_ok`), no digits, no title-ish stop words."""
    s = s.strip(" ,.;:-")
    if not s or len(s) > 50 or re.search(r"\d|[@/\\]", s):
        return False
    toks = s.replace(",", " ").split()
    if not (1 if single_ok else 2) <= len(toks) <= 5:
        return False
    for t in toks:
        w = t.strip(".'-").lower()
        if not w or w in _NAME_STOP:
            return False
        if not (t[0].isupper() or w in {"de", "van", "von", "der", "da", "di", "la", "le", "del", "bin", "ibn", "al"}):
            return False
        if not re.fullmatch(r"[^\W\d_]+(?:[.'\u2019-][^\W\d_]*)*\.?", t):
            return False
    return True


def _clean_author(s: str) -> str:
    s = re.sub(r"\s+\d{1,4}\s*$", "", s.strip())                 # trailing page number
    s = re.sub(r",?\s+(M\.?A\.?|Ph\.?D\.?|M\.?D\.?|B\.?A\.?)\W*$", "", s, flags=re.I).strip(" ,.;:-")
    if s.isupper():
        s = s.title()
    return s


def author_from_pages(text: str, title: str = "") -> str:
    """Author from the title pages: 'Author: X', a 'by X' line, 'Title, by X', or a name line right under the title."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    for l in lines[:80]:
        m = re.match(r"^(?:author|written by|autor)s?\s*:\s*(.+)$", l, re.I)
        if m and looks_like_name(_clean_author(m.group(1)), single_ok=True):
            return _clean_author(m.group(1))
    for i, l in enumerate(lines[:80]):
        prev = lines[i - 1] if i else ""
        if _ROLE_BEFORE.search(prev):
            continue
        m = re.match(r"^by\s+(.{3,60})$", l, re.I) or re.match(r"^.{3,90},\s+by\s+(.{3,60})$", l, re.I)
        if m:
            cand = _clean_author(m.group(1))
            if looks_like_name(cand) and not re.match(r"^(translated|edited)\b", l, re.I):
                return cand
        if re.fullmatch(r"by", l, re.I) and i + 1 < len(lines):      # "BY" alone, name on the next line
            cand = _clean_author(lines[i + 1])
            if looks_like_name(cand) and not _ROLE_BEFORE.search(prev):
                return cand
    if title:
        tk = re.sub(r"\W+", " ", title).strip().lower()
        for i, l in enumerate(lines[:12]):
            if tk and re.sub(r"\W+", " ", l).strip().lower() == tk and i + 1 < len(lines):
                cand = _clean_author(lines[i + 1])
                if looks_like_name(cand):
                    return cand
                break
    return ""


def title_author_from_filename(path: Path) -> tuple[str, str]:
    """'Title - Author.pdf' (or 'Author - Title.pdf' when only the left side looks like a name) -> (title, author)."""
    stem = re.sub(r"_+", " ", path.stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    parts = [p.strip() for p in re.split(r"\s+[-\u2013\u2014]+\s+|\s+--\s+", stem) if p.strip()]
    if len(parts) < 2:
        return "", ""
    left, right = parts[0], parts[1]
    if looks_like_name(right) or not looks_like_name(left):
        title, author = left, right
    else:
        title, author = right, left
    if not looks_like_name(author) and not re.fullmatch(r"[^\W\d_]{3,}(?: [^\W\d_.]{2,})?", author):
        return "", ""
    pretty = lambda t: t.title() if t.islower() or t.isupper() else t
    return pretty(title), pretty(author)


def pick_title_author(info: dict[str, str], first_page: str, path: Path) -> tuple[str, str]:
    """Metadata first; then title-page text ('Author: X', 'by X', a name under the title); then the file name
    ('Title - Author.pdf'). `first_page` may hold several title pages' text. The author is 'Unknown' only as a last resort."""
    title = info.get("Title", "").strip()
    author = info.get("Author", "").strip()
    if not title or _JUNK_TITLE.match(title):
        title = ""
    if author and _JUNK_AUTHOR.match(author):
        author = ""
    lines = [l.strip() for l in first_page.splitlines() if l.strip()]
    if not author:
        for i, l in enumerate(lines[:12]):
            m = re.match(r"^by\s+(.{3,60})$", l, re.I)
            if m and looks_like_name(_clean_author(m.group(1))) and not (i and _ROLE_BEFORE.search(lines[i - 1])):
                author = _clean_author(m.group(1))
                if not title and i > 0 and len(lines[i - 1]) < 80:
                    title = display_title(lines[i - 1])
                break
    if not author:
        author = author_from_pages(first_page, title)
    f_title, f_author = title_author_from_filename(path)
    if not author:
        author = f_author
    if not title:
        title = f_title or pretty_stem(path)
    return title, author or "Unknown"


# ------------------------------------------------------------------ assembly

def _trim_blank(lines: list[str]) -> list[str]:
    a, b = 0, len(lines)
    while a < b and not lines[a].strip():
        a += 1
    while b > a and not lines[b - 1].strip():
        b -= 1
    return lines[a:b]


def clean_pages(pages: list[str]) -> tuple[list[list[str]], dict]:
    """Page texts -> per-page line lists with front/back matter pages emptied, headers/footers/footnotes removed."""
    stats = {"toc_pages": 0, "copyright_pages": 0, "index_pages": 0, "header_lines": 0}
    lp = [p.replace("\r", "").split("\n") for p in pages]
    n = len(lp)
    lp = [_trim_blank(l) for l in lp]
    for i, lines in enumerate(lp):
        if i < min(15, n) and is_toc_page(lines):
            stats["toc_pages"] += 1
            lp[i] = []
        elif i < min(8, n) and is_copyright_page(lines):
            stats["copyright_pages"] += 1
            lp[i] = []
    # trailing index pages
    j = n - 1
    while j > 0 and (not lp[j] or is_index_page(lp[j])):
        if lp[j]:
            stats["index_pages"] += 1
        lp[j] = []
        j -= 1
    lp = [_trim_blank(remove_footnotes(l)) for l in lp]          # first: a footnote block must not be mistaken for a repeated footer
    lp, stats["header_lines"] = remove_headers_footers(lp)
    lp = [_trim_blank(l) for l in lp]
    return lp, stats


def split_parts(paras: list[str], words_per_part: int | None = None) -> list[Chapter]:
    target = words_per_part or config.PDF_PART_WORDS
    parts: list[list[str]] = []
    cur: list[str] = []
    w = 0
    for p in paras:
        cur.append(p)
        w += len(p.split())
        if w >= target:
            parts.append(cur)
            cur, w = [], 0
    if cur:
        if parts and w < target * 0.3:
            parts[-1].extend(cur)
        else:
            parts.append(cur)
    return [Chapter(f"Part {i}", ps, "pdf") for i, ps in enumerate(parts, 1)]


def segment(lp: list[list[str]], outline: list[tuple[int, str, int]] | None = None,
            max_chapters: int | None = None) -> tuple[list[tuple[str, list[tuple[int, str]]]], list[tuple[str, str]], str]:
    """Cleaned pages -> ([(title, flat lines)], dropped, mode). mode: outline | headings | parts."""
    flat: list[tuple[int, str]] = []
    first_line: dict[int, int] = {}
    for pi, lines in enumerate(lp):
        if any(l.strip() for l in lines):
            first_line[pi] = len(flat)
        flat.extend((pi, l) for l in lines)
    dropped: list[tuple[str, str]] = []
    heads = headings_from_outline(outline or [], flat, first_line)
    mode = "outline"
    if len([h for h in heads if not h.matter]) < 2:
        heads = detect_headings(flat)
        mode = "headings"
    real = [h for h in heads if not h.matter]
    if len(real) < 2:
        return [("", flat)], dropped, "parts"
    sections: list[tuple[str, list[tuple[int, str]]]] = []
    if heads[0].line > 0:
        pre = flat[:heads[0].line]
        words = sum(len(l.split()) for _, l in pre)
        if words:
            dropped.append(("front matter", f"{words} words before the first chapter heading"))
    for k, h in enumerate(heads):
        end = heads[k + 1].line if k + 1 < len(heads) else len(flat)
        body = flat[h.line + h.skip:end]
        if h.matter:
            dropped.append((h.title, "front/back matter"))
            continue
        sections.append((h.title, body))
    return sections, dropped, mode


def build_book(path: Path, pages: list[str], info: dict[str, str], outline: list[tuple[int, str, int]],
               cleaner: Cleaner | None, max_chapters: int | None = None, cover: bytes | None = None) -> Book:
    lp, stats = clean_pages(pages)
    title, author = pick_title_author(info, "\n".join(pages[:3]), path)
    sections, dropped, mode = segment(lp, outline, max_chapters)
    log.info("pdf: %d pages, %s; chapters via %s; stats %s", len(pages), title, mode, stats)

    chapters: list[Chapter] = []
    if mode == "parts":
        paras = rebuild_paragraphs(sections[0][1])
        # a short, text-free first page is a title page
        parts = split_parts(paras)
        if max_chapters:
            parts = parts[:max_chapters]
        chapters = parts
    else:
        for ttl, body in sections:
            if max_chapters and len(chapters) >= max_chapters:
                break
            paras = rebuild_paragraphs(body)
            if sum(len(p.split()) for p in paras) < 15:
                dropped.append((ttl, "almost no text"))
                continue
            chapters.append(Chapter(ttl or f"Section {len(chapters) + 1}", paras, "pdf"))
    if not chapters:
        raise ValueError("no readable text found in PDF after cleanup")

    if cleaner is not None and cleaner.providers:
        for ch, paras in zip(chapters, cleaner.clean_chapters([c.paragraphs for c in chapters])):
            ch.paragraphs = paras
    for ch in chapters:
        ch.paragraphs = [p for p in (normalize_text(t) for t in ch.paragraphs) if p]
    chapters = [c for c in chapters if c.paragraphs]
    if not chapters:
        raise ValueError("no readable text found in PDF after cleanup")
    book = Book(title=title, author=author, chapters=chapters, cover=cover, cover_ext="jpg", dropped=dropped)
    book.llm_provider = cleaner.provider_label if cleaner is not None else "none"
    return book


def nonspace_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def pages_with_images(path: Path) -> set[int] | None:
    """0-based pages that carry at least one raster image (pdfimages -list); None when that cannot be determined."""
    try:
        r = _run(["pdfimages", "-list", str(path)], timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out: set[int] = set()
    for line in r.stdout.decode("utf-8", "replace").splitlines()[2:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            out.add(int(parts[0]) - 1)
    return out


def ocr_candidates(path: Path, pages: list[str]) -> list[int]:
    """Pages that are pictures of text: (almost) no text layer but a raster image. Blank vector pages are left alone."""
    thin = [i for i, p in enumerate(pages) if nonspace_chars(p) < MIN_PAGE_CHARS]
    if not thin:
        return []
    imgs = pages_with_images(path)
    return thin if imgs is None else [i for i in thin if i in imgs]


def ocr_pdf_pages(path: Path, pages: list[str], need: list[int], opts=None) -> tuple[list[str], Path | None]:
    """OCR the pages `need` of a PDF and return (all page texts with those replaced, searchable PDF or None)."""
    from .. import ocr
    from ..ocr.postprocess import clean_ocr_pages
    lang = getattr(opts, "ocr_lang", None) or None
    res = ocr.run_ocr(path, lang, getattr(opts, "ocr_workers", None) or None, backend=getattr(opts, "ocr_backend", None) or None,
                      pages=need, on_event=getattr(opts, "on_event", None))
    texts = clean_ocr_pages([pt.text for pt in res.pages], lang or ocr_lang_setting())
    out = list(pages)
    for pt, t in zip(res.pages, texts):
        out[pt.index] = t
    return out, res.searchable_pdf


def ocr_lang_setting() -> str:
    return config.get_settings().ocr_lang


def extract_pdf(path: str | Path, max_chapters: int | None = None, llm: str = "auto",
                cleaner: Cleaner | None = None, on_event=None, opts=None) -> Book:
    path = Path(path)
    info = pdf_info(path)
    pages = pdf_pages(path)
    if not pages:
        raise ValueError("this PDF has no pages")
    need = ocr_candidates(path, pages)
    searchable = None
    if need:
        log.info("pdf: %d of %d pages are images without text; running OCR", len(need), len(pages))
        if on_event is not None and opts is not None and getattr(opts, "on_event", None) is not on_event:
            opts = _EventOpts(opts, on_event)
        elif opts is None:
            opts = _EventOpts(None, on_event)
        try:
            pages, searchable = ocr_pdf_pages(path, pages, need, opts)
        except Exception as e:  # noqa: BLE001
            from ..ocr import OcrError
            if isinstance(e, OcrError):
                raise ScannedPdfError(f"this PDF is scanned and needs OCR, which could not run: {e}") from e
            raise
    chars = sum(nonspace_chars(p) for p in pages)
    if chars / len(pages) < MIN_CHARS_PER_PAGE and not need:
        raise ScannedPdfError(f"no readable text found in this PDF ({chars} characters over {len(pages)} pages)")
    if cleaner is None:
        cleaner = Cleaner.from_choice(llm or "auto")
    if on_event is not None:
        cleaner.on_event = on_event
    book = build_book(path, pages, info, pdf_outline(path), cleaner, max_chapters, pdf_cover(path))
    if searchable is not None:
        book.artifacts["searchable_pdf"] = searchable
    return book


class _EventOpts:
    """opts wrapper that swaps in an on_event hook."""

    def __init__(self, base, on_event):
        self._base, self.on_event = base, on_event

    def __getattr__(self, name):
        return getattr(self._base, name, None)


# ------------------------------------------------------------------ extractor plugin interface

extensions = (".pdf",)


def can_handle(path) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def extract(path, opts=None) -> Book:
    return extract_pdf(path, getattr(opts, "max_chapters", None), llm=getattr(opts, "llm", None) or "auto",
                       on_event=getattr(opts, "on_event", None), opts=opts)


def formats():
    from .common import FormatInfo
    return [FormatInfo("PDF", (".pdf",), "poppler pdftotext (+ optional LLM cleanup); scanned pages via OCR (" + _ocr_backend_label() + ")",
                       "poppler-utils", shutil.which("pdftotext") is not None,
                       "" if shutil.which("pdftotext") else "install poppler (pdftotext)")]


def _ocr_backend_label() -> str:
    from ..ocr import describe
    d = describe()
    return {"ocrmypdf": "ocrmypdf", "tesseract": "Tesseract", "none": "not installed"}[d["pdf_backend"]]
