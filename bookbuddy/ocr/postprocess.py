"""OCR-specific text fixes that run before the normal PDF heuristics (headers, page numbers, hyphenation, chapters).

`fix_ocr_text` repairs one page; `strip_running_heads` removes running headers and page numbers across the OCR'd pages
even when OCR read them slightly differently on each page. Rules that assume English (single stray letters, word
substitutions, spacing before punctuation) apply only when the language is English.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st", "Ĳ": "IJ", "ĳ": "ij",
             "Œ": "OE", "œ": "oe", "Æ": "AE", "æ": "ae"}
_LIG_RE = re.compile("|".join(map(re.escape, LIGATURES)))
# only ligatures Latin-script English text never uses; Œ/Æ stay in other languages' text, so they are handled for English only
_ALWAYS_LIG = {k: v for k, v in LIGATURES.items() if k in "ﬁﬂﬀﬃﬄﬅﬆ"}

# typical old-book / low-quality OCR misreadings that are never real English words
_WORD_FIXES = {
    "tbe": "the", "tlie": "the", "thc": "the", "tiie": "the", "wliich": "which", "wbich": "which", "whicb": "which",
    "witli": "with", "wilh": "with", "tliat": "that", "tbat": "that", "tliis": "this", "tbis": "this", "liad": "had",
    "liave": "have", "bave": "have", "liis": "his", "bis": "his", "lier": "her", "wben": "when", "wlien": "when",
    "tbey": "they", "tliey": "they", "tbere": "there", "tliere": "there", "wbat": "what", "wliat": "what", "aud": "and",
    "arid": "and", "bnt": "but", "hut": "but", "bad": "bad", "ot": "of", "tbem": "them", "tbem.": "them.",
    "lt": "It", "ln": "In", "ls": "Is", "Tbe": "The", "Tlie": "The", "Tbat": "That", "Tliat": "That", "Wben": "When",
    "Wlien": "When", "Wbat": "What", "Wliat": "What", "Aud": "And", "Hnt": "But", "Bnt": "But",
}
_WORD_FIXES = {k: v for k, v in _WORD_FIXES.items() if k not in ("hut", "bad", "arid", "bis", "ot", "tbem.")}   # real words: leave alone
_WORD_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _WORD_FIXES), key=len, reverse=True)) + r")\b")

_STRAY_SYMBOL = re.compile(r"(?:(?<=\s)|^)[~^_*|\\<>©®°•·¢§¶=+#@`]{1}(?=\s|$)")
_LONE_LETTER = re.compile(r"(?<=[a-z,;])\s[b-hj-np-z](?=\s[a-z])")
_ORNAMENT_LINE = re.compile(r"^[\s*•·.\-–—_=~°+#]*$")
_PAGE_NUMBERISH = re.compile(r"^[\[\(\-–—~\s]*([0-9lIiOo|]{1,4})[\]\)\-–—.\s]*$")
_VERBS_AFTER_I = r"am|was|had|have|will|would|shall|should|think|know|do|did|can|could|must|may|might|see|saw|said|am|thought|felt"


def is_english(lang: str) -> bool:
    return (lang or "eng").lower().split("+")[0] in ("eng", "en", "enm")


def fix_ocr_text(text: str, lang: str = "eng") -> str:
    """Repair one OCR'd page: ligatures, line-end hyphen marks, '|' for I, stray marks and lines, common misreadings."""
    if not text:
        return ""
    eng = is_english(lang)
    t = unicodedata.normalize("NFC", text.replace("\r", "").replace("\x0c", ""))
    t = t.replace("­", "")
    t = _LIG_RE.sub(lambda m: (_ALWAYS_LIG.get(m.group(0)) or (LIGATURES[m.group(0)] if eng else m.group(0))), t)
    # line-end hyphen look-alikes (OCR often reads the typographic hyphen as ¬ or a Unicode hyphen)
    t = re.sub(r"[¬‐‑‒](?=[ \t]*\n)", "-", t)
    out_lines: list[str] = []
    for raw in t.split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if s and (_ORNAMENT_LINE.match(s) or (len(s) <= 12 and sum(c.isalnum() for c in s) / len(s) < 0.34)):
            line = ""                                   # scan specks and decorative rules become a paragraph gap
        elif s and noisy_line(s):
            line = ""
        elif len(s) == 1 and s.isalpha() and s not in "aAIoO" and eng:
            line = ""
        else:
            line = _fix_line(line, eng)
        out_lines.append(line)
    t = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", t).strip("\n")


_QUOTE_RUN = re.compile("[\"'`‘’“”]{2,3}")      # OCR reads a curly quote as two or three straight marks


_HEAD_GLUE = re.compile(r"^(\s*)(CHAPTER|Chapter|PART|Part|BOOK|Book|SECTION|Section|LETTER|Letter|CANTO|Canto)(?=[IVXLCivxlc1-9]{1,6}\b)")


_JUNK_CHARS = set("\\_&^~|<>{}[]=*#@%$`´¬°§¶")


def noisy_line(s: str) -> bool:
    """A line that is scan noise (a dark cover, a stain, a stamp), not text: many symbol characters, or mostly 1-2 character bits."""
    body = re.sub(r"\s+", "", s)
    if len(body) < 4:
        return False
    junk = sum(c in _JUNK_CHARS for c in body)
    if junk / len(body) >= 0.15:
        return True
    toks = s.split()
    if len(toks) >= 5 and sum(len(t.strip(".,;:!?'\"")) <= 2 for t in toks) / len(toks) >= 0.7 and not re.search(r"[a-z]{4,}", s):
        return True
    punct = sum(not c.isalnum() for c in body)
    if punct / len(body) >= 0.2 and not re.search(r"[a-z]{4,}", s):
        return True
    letters = sum(c.isalpha() for c in body)
    return len(body) >= 6 and letters / len(body) < 0.45


def _fix_line(line: str, eng: bool) -> str:
    line = _HEAD_GLUE.sub(r"\1\2 ", line)                 # "CHAPTER1" -> "CHAPTER 1" (OCR loses the narrow space)
    # '|' (or lower-case l) read for a capital I
    line = re.sub(r"(?<![\w|])\|(?=[a-z]{1,2}\b)", "I", line)          # |t, |n, |f
    line = re.sub(r"(?<![\w|])\|(?=['’])", "I", line)                   # |'ll
    line = re.sub(r"(?<![\w|])\|(?=\s+[a-z])", "I", line)               # | am
    line = re.sub(r"(?<![\w|])\|(?=[A-Za-z])", "I", line)               # |ndeed
    line = re.sub(r"(?<![\w|])\|(?=[.,;:!?\s]|$)", "I", line) if re.search(r"\|\s", line) and not re.search(r"\|.*\|", line) else line
    if eng:
        line = re.sub(rf"(?<![\w'])l(?=['’](?:m|ll|ve|d)\b)", "I", line)    # l'm, l'll
        line = re.sub(rf"(?<![\w'])l(?=\s+(?:{_VERBS_AFTER_I})\b)", "I", line)   # l am
        line = _WORD_RE.sub(lambda m: _WORD_FIXES[m.group(0)], line)
        line = re.sub(r"\s+([,.;:!?])(?=\s|$)", r"\1", line)
        line = _LONE_LETTER.sub("", line)
    line = _STRAY_SYMBOL.sub("", line)
    line = _QUOTE_RUN.sub('"', line)
    return re.sub(r"[ \t]{2,}", " ", line).strip() if line.strip() else ""


def garbage_page(text: str) -> bool:
    """A page whose OCR is mostly noise (a picture, a blank scan): too few letters, or too few letters per character."""
    body = re.sub(r"\s+", "", text)
    if len(body) < 20:
        return True
    letters = sum(c.isalpha() for c in body)
    return letters / len(body) < 0.5


# ------------------------------------------------------------------ running heads and page numbers

def _digit_fix(s: str) -> str:
    return s.translate(str.maketrans({"l": "1", "I": "1", "i": "1", "|": "1", "O": "0", "o": "0"}))


def _head_key(line: str) -> str:
    s = re.sub(r"\s+", " ", line.strip().lower())
    s = re.sub(r"^[\W_]*\d{1,4}\b|\b\d{1,4}[\W_]*$", "#", s)
    return re.sub(r"[^a-z#]+", " ", s).strip()


def strip_running_heads(pages: list[str]) -> list[str]:
    """Remove page-number lines and running headers/footers from OCR'd page texts. Header lines are matched fuzzily
    (OCR may read 'THE ADVENTURES OF' as 'THE ADVENTURFS OF' on one page), so they still count as the same line."""
    n = len(pages)
    if n == 0:
        return pages
    lines = [p.split("\n") for p in pages]
    edge_idx: list[list[int]] = []
    for ls in lines:
        idx = [i for i, l in enumerate(ls) if l.strip()]
        edge_idx.append(sorted(set(idx[:2] + idx[-2:])))
    need = max(3, int(n * 0.25 + 0.5)) if n >= 6 else max(2, n // 2 + 1) if n >= 3 else 99
    clusters: list[tuple[str, int, set[int]]] = []          # (key, count, pages)
    cand: dict[tuple[int, int], int] = {}
    for pi, idxs in enumerate(edge_idx):
        for i in idxs:
            line = lines[pi][i]
            if re.match(r"^\s*(chapter|part|book|section|canto|letter)\s+[\dIVXLC]+", line, re.I) and len(line) < 60:
                continue
            key = _head_key(line)
            if not 3 <= len(key.replace("#", "").strip()) <= 70:         # running heads are short; long edge lines are body text
                continue
            for ci, (ck, cnt, pset) in enumerate(clusters):
                if abs(len(ck) - len(key)) <= max(3, len(key) // 5) and difflib.SequenceMatcher(None, ck, key).ratio() >= 0.82:
                    if pi not in pset:
                        clusters[ci] = (ck, cnt + 1, pset | {pi})
                    cand[(pi, i)] = ci
                    break
            else:
                clusters.append((key, 1, {pi}))
                cand[(pi, i)] = len(clusters) - 1
    repeated = {ci for ci, (_, cnt, _) in enumerate(clusters) if cnt >= need}
    out: list[str] = []
    for pi, ls in enumerate(lines):
        drop = {i for (p, i), ci in cand.items() if p == pi and ci in repeated}
        for i in edge_idx[pi]:
            m = _PAGE_NUMBERISH.match(ls[i])
            if m:
                tok = m.group(1)
                if re.search(r"\d", tok) or len(tok) >= 2 and set(tok) <= set("lIiOo|1"):
                    drop.add(i)
        out.append("\n".join(l for i, l in enumerate(ls) if i not in drop).strip("\n"))
    return out


# ------------------------------------------------------------------ chapter numbers

_KW = r"CHAPTER|Chapter|PART|Part|BOOK|Book|LETTER|Letter|CANTO|Canto"
_HEAD = re.compile(rf"^(?P<pre>\s*(?P<kw>{_KW}))(?:\s+(?P<num>[0-9IlL|iVXCDM]{{1,6}}))?(?P<rest>[.:,]?\s*.*)$")
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _roman_value(tok: str) -> int | None:
    if not re.fullmatch(r"[IVXLCDM]+", tok):
        return None
    total = 0
    for a, b in zip(tok, tok[1:] + " "):
        v = _ROMAN[a]
        total += -v if b in _ROMAN and _ROMAN[b] > v else v
    return total


def _to_roman(n: int) -> str:
    out = ""
    for v, r in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                 (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out += r
            n -= v
    return out


def repair_chapter_numbers(pages: list[str]) -> list[str]:
    """'CHAPTER 11' / 'CHAPTER I1' read for 'CHAPTER II', or a bare 'CHAPTER' whose numeral OCR lost: restore the number
    from the sequence of chapter headings in the book (Roman or Arabic, whichever the book uses)."""
    lines = [p.split("\n") for p in pages]
    hits: list[tuple[int, int, re.Match]] = []
    for pi, ls in enumerate(lines):
        nonblank = [i for i, l in enumerate(ls) if l.strip()]
        for i in nonblank[:4]:                      # chapter headings sit at the top of a page here
            m = _HEAD.match(ls[i])
            if not m or len(ls[i]) > 70:
                continue
            if not m.group("num") and ls[i].strip() != m.group("kw"):
                continue
            hits.append((pi, i, m))
    if not hits:
        return pages
    toks = [m.group("num") or "" for _, _, m in hits]
    roman_style = any(t and _roman_value(t) is not None and not re.fullmatch(r"[1Il|i]", t) or bool(re.fullmatch(r"[1Il|i]{2,}", t)) for t in toks)
    roman_style = roman_style and not any(re.fullmatch(r"[2-9]\d*", t) for t in toks)
    expect = 0
    for (pi, i, m), tok in zip(hits, toks):
        expect += 1
        kw, rest = m.group("kw"), m.group("rest")
        val = _roman_value(tok) if tok else None
        if val is None and tok and tok.isdigit() and not roman_style:
            val = int(tok)
        if val is not None and (val == expect or tok not in ("1", "I")):          # a clean numeral is trusted
            expect = val
            continue
        new = _to_roman(expect) if roman_style else str(expect)
        sep = "" if rest.startswith((".", ":", ",")) else (" " if rest and not rest.startswith(" ") else "")
        lines[pi][i] = f"{m.group('pre')} {new}{sep}{rest}".rstrip()
    return ["\n".join(ls) for ls in lines]


def clean_ocr_pages(texts: list[str], lang: str = "eng") -> list[str]:
    """Per-page fixes, noise pages emptied, running heads/page numbers stripped. Same length and order as the input."""
    fixed = [fix_ocr_text(t, lang) for t in texts]
    fixed = repair_chapter_numbers(strip_running_heads(fixed))
    return ["" if garbage_page(t) else t for t in fixed]
