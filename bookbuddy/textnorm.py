"""Text normalisation for speech, sentence splitting and chunking."""
from __future__ import annotations

import re
from dataclasses import dataclass

from num2words import num2words

from . import config

# ---------------------------------------------------------------- numbers

_ONES_YEAR = {0: "oh"}


def _cardinal(n: int) -> str:
    return num2words(n).replace(" and ", " ").replace(",", "")


def _ordinal(n: int) -> str:
    return num2words(n, to="ordinal").replace(" and ", " ").replace(",", "")


def year_to_words(y: int) -> str:
    """1984 -> 'nineteen eighty-four', 1905 -> 'nineteen oh five', 2005 -> 'two thousand five'."""
    if y == 2000:
        return "two thousand"
    if 2000 < y < 2010:
        return "two thousand " + _cardinal(y - 2000)
    hi, lo = divmod(y, 100)
    if lo == 0:
        return f"{_cardinal(hi)} hundred"
    if lo < 10:
        return f"{_cardinal(hi)} oh {_cardinal(lo)}"
    return f"{_cardinal(hi)} {_cardinal(lo)}"


def _decade(m: re.Match) -> str:
    y = int(m.group(1))
    base = year_to_words(y)
    if y % 10 == 0 and y % 100 != 0:
        words = base.rsplit(" ", 1)
        last = words[-1]
        plural = last[:-1] + "ies" if last.endswith("y") else last + "s"
        return " ".join(words[:-1] + [plural])
    return base + "s"


def _num_token(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        whole, frac = s.split(".", 1)
        return f"{_cardinal(int(whole or 0))} point " + " ".join(_cardinal(int(c)) for c in frac)
    return _cardinal(int(s))


def _currency(m: re.Match) -> str:
    whole = int(m.group(1).replace(",", ""))
    cents = m.group(2)
    out = f"{_cardinal(whole)} dollar{'' if whole == 1 else 's'}"
    if cents and int(cents[1:]) > 0:
        c = int(cents[1:])
        out += f" and {_cardinal(c)} cent{'' if c == 1 else 's'}"
    return out


def _time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if mi == 0:
        return f"{_cardinal(h)} o'clock"
    if mi < 10:
        return f"{_cardinal(h)} oh {_cardinal(mi)}"
    return f"{_cardinal(h)} {_cardinal(mi)}"


# ---------------------------------------------------------------- abbreviations

_TITLE_ABBR = {
    "Mr": "Mister", "Mrs": "Missus", "Ms": "Miz", "Dr": "Doctor", "Prof": "Professor",
    "Rev": "Reverend", "Capt": "Captain", "Col": "Colonel", "Gen": "General", "Sgt": "Sergeant",
    "Lt": "Lieutenant", "Hon": "Honorable", "Gov": "Governor", "Sen": "Senator",
    "Mt": "Mount", "Ft": "Fort", "Jr": "Junior", "Sr": "Senior", "Messrs": "Messiers",
}
_TITLE_RE = re.compile(r"\b(" + "|".join(_TITLE_ABBR) + r")\.")


def _st(m: re.Match) -> str:
    # "St. Paul" -> Saint ; "Main St." / "St." before lowercase/end -> Street
    after = m.string[m.end():m.end() + 2]
    return "Saint" if re.match(r"\s[A-Z]", after) and not re.search(r"[A-Z][a-z]+ $", m.string[:m.start()] + " ") else "Street"


def expand_abbreviations(t: str) -> str:
    t = _TITLE_RE.sub(lambda m: _TITLE_ABBR[m.group(1)], t)
    t = re.sub(r"\bSt\.", _st, t)
    t = re.sub(r"\bNo\.\s*(?=\d)", "Number ", t)
    t = re.sub(r"\bvs\.", "versus", t)
    t = re.sub(r"\be\.g\.", "for example", t)
    t = re.sub(r"\bi\.e\.", "that is", t)
    t = re.sub(r"\betc\.(?=\s+[A-Z\"'“‘]|\s*$)", "et cetera.", t)
    t = re.sub(r"\betc\.", "et cetera", t)
    t = re.sub(r"\b([ap])\.m\.", lambda m: m.group(1).upper() + " M", t, flags=re.I)
    t = re.sub(r"&", " and ", t)
    return t


# ---------------------------------------------------------------- footnotes / symbols

_SUPERSCRIPTS = str.maketrans("", "", "⁰¹²³⁴⁵⁶⁷⁸⁹†‡§¶")


def strip_markers(t: str) -> str:
    t = re.sub(r"\[\s*\d{1,3}\s*\]", "", t)            # [12]
    t = re.sub(r"\[\s*[a-z]\s*\]", "", t)              # [a]
    t = re.sub(r"\[\s*[*†‡]+\s*\]", "", t)
    t = re.sub(r"(?<=[A-Za-z.,;:!?”\"’'])\*+", "", t)  # trailing asterisks
    t = t.translate(_SUPERSCRIPTS)
    t = re.sub(r"\{\d+\}", "", t)
    t = t.replace("_", "")                              # Gutenberg italics markers
    return t


# ---------------------------------------------------------------- main normaliser

def normalize_text(t: str) -> str:
    """One block (paragraph) of raw text -> speakable text."""
    t = t.replace(" ", " ").replace("​", "").replace("­", "")
    t = strip_markers(t)
    # quotes -> ASCII
    t = t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    t = t.replace("«", '"').replace("»", '"')
    t = expand_abbreviations(t)
    # numbers
    t = re.sub(r"\$(\d[\d,]*)(\.\d\d)?", _currency, t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s?%", lambda m: _num_token(m.group(1)) + " percent", t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b(?!:)", _time, t)
    t = re.sub(r"\b(1[0-9]{3}|20[0-9]{2})s\b", _decade, t)
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: _ordinal(int(m.group(1))), t)
    t = re.sub(r"(?<![\d,.$])\b(1[1-9][0-9]{2}|20[0-9]{2})\b(?![,.]\d)", lambda m: year_to_words(int(m.group(1))), t)
    t = re.sub(r"(\d)\s?[–-]\s?(\d)", r"\1 to \2", t)
    t = re.sub(r"\d[\d,]*(?:\.\d+)?", lambda m: _num_token(m.group(0).rstrip(",")) if m.group(0)[-1:] != "," else _num_token(m.group(0)[:-1]) + ",", t)
    # dashes -> a spoken pause; keep sentence punctuation intact
    t = re.sub(r"\s*[—–]\s*", ", ", t)
    t = re.sub(r"\s--+\s*|--+", ", ", t)
    t = re.sub(r"([,;:])\s*,+", r"\1", t)
    t = re.sub(r",\s*([.!?])", r"\1", t)
    t = re.sub(r"\.{4,}", "...", t)
    t = t.replace("…", "...")
    t = re.sub(r"\s+", " ", t).strip()
    # scene-break glyph lines (* * *, ***, ---) are not speech
    if re.fullmatch(r"[\W_]*", t) and not re.search(r"\w", t):
        return ""
    return t


# ---------------------------------------------------------------- sentence splitting / chunking

_SENT_END = re.compile(r"""([.!?]+|\.\.\.)(["')\]]*)(\s+)(?=["'(\[]?[A-Z0-9])""")
_INITIAL = re.compile(r"(?:^|\s)[A-Z]\.$")


def split_sentences(para: str) -> list[str]:
    """Split a paragraph into sentences; never splits inside a sentence."""
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(para):
        head = para[start:m.start() + len(m.group(1))]
        # "J. R. Smith": a lone capital + '.' is an initial, not a sentence end
        if m.group(1) == "." and not m.group(2) and _INITIAL.search(head):
            continue
        out.append(para[start:m.end(2)].strip())
        start = m.end()
    tail = para[start:].strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]


def _split_long(sentence: str, limit: int) -> list[str]:
    """A single sentence over the cap: break at clause boundaries, then at spaces (last resort)."""
    if len(sentence) <= limit:
        return [sentence]
    parts: list[str] = []
    cur = ""
    pieces = re.split(r"(?<=[,;:])\s+", sentence)
    for p in pieces:
        while len(p) > limit:  # no punctuation: split at a space
            cut = p.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + 1 + len(p) > limit:
            parts.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        parts.append(cur)
    return parts


@dataclass
class Chunk:
    chapter: int
    index: int
    text: str
    pause: float        # seconds of silence after this chunk
    kind: str = "text"  # "title" | "text"
    # anchor back into the book text: (chapter, paragraph) plus character offsets inside that paragraph.
    # chapter is `Chunk.chapter`; the title chunk has no paragraph. start/end are None if the text was reworded.
    paragraph: int | None = None
    start: int | None = None
    end: int | None = None


def chunk_paragraph(para: str, limit: int = config.MAX_CHUNK_CHARS, min_len: int = 20) -> list[tuple[str, float]]:
    """Paragraph -> [(chunk_text, pause_after)]. Very short sentences merge into the next one."""
    sents: list[str] = []
    for s in split_sentences(para):
        sents.extend(_split_long(s, limit))
    merged: list[str] = []
    carry = ""
    for s in sents:
        cand = f"{carry} {s}".strip() if carry else s
        if len(cand) < min_len and s is not sents[-1]:
            carry = cand
            continue
        if len(cand) > limit and carry:
            merged.append(carry)
            cand = s
        merged.append(cand)
        carry = ""
    if carry:
        merged.append(carry)
    return [(m, config.PAUSE_SENTENCE) for m in merged]


def _locate(para: str, piece: str, cursor: int) -> tuple[int, int] | None:
    """Character span of `piece` in `para` at or after `cursor` (whitespace-insensitive), or None."""
    words = piece.split()
    if not words:
        return None
    m = re.compile(r"\s+".join(re.escape(w) for w in words)).search(para, cursor)
    return (m.start(), m.end()) if m else None


def chunk_chapter(chapter_idx: int, title: str, paragraphs: list[str]) -> list[Chunk]:
    chunks: list[Chunk] = []
    if title:
        chunks.append(Chunk(chapter_idx, 0, title.rstrip(".") + ".", config.PAUSE_AFTER_TITLE, "title"))
    for pi, para in enumerate(paragraphs):
        pieces = chunk_paragraph(para)
        cursor = 0
        for i, (txt, pause) in enumerate(pieces):
            if i == len(pieces) - 1:
                pause = config.PAUSE_PARAGRAPH
            span = _locate(para, txt, cursor)
            if span:
                cursor = span[1]
            chunks.append(Chunk(chapter_idx, len(chunks), txt, pause, paragraph=pi,
                                start=span[0] if span else None, end=span[1] if span else None))
    if chunks:
        chunks[-1].pause = config.PAUSE_CHAPTER_END
    return chunks


# ---------------------------------------------------------------- chapter titles

_ROMAN = re.compile(r"^(?=[IVXLCDM]+$)M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_ROMAN_VALS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s: str) -> int:
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _ROMAN_VALS[a]
        total += -v if b in _ROMAN_VALS and _ROMAN_VALS[b] > v else v
    return total


def display_title(raw: str) -> str:
    """Clean a TOC title: collapse whitespace, de-shout, 'CHAPTER IV.' -> 'Chapter 4.'"""
    t = re.sub(r"\s+", " ", raw or "").strip()
    if t.isupper() and len(t) > 3:
        t = t.title()
        t = re.sub(r"(\w)'S\b", r"\1's", t)
    t = t.replace("’", "'")
    t = re.sub(r"\b(Chapter|Part|Book|Section|Canto)\s+([IVXLCDM]+)\b",
               lambda m: f"{m.group(1).capitalize()} {roman_to_int(m.group(2).upper())}" if _ROMAN.match(m.group(2).upper()) else m.group(0),
               t, flags=re.I)
    return t


def spoken_title(title: str) -> str:
    """Title as spoken: digits to words ('Chapter 4. Pig' -> 'Chapter four. Pig')."""
    t = normalize_text(title)
    t = re.sub(r"\s+", " ", t).strip()
    return t
