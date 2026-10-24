"""Plain text (.txt): encoding detection, Project Gutenberg header/footer stripping, chapter headings, "Part N" fallback."""
from __future__ import annotations

import re
from pathlib import Path

from ..book import Book, Chapter
from ..textnorm import display_title, normalize_text
from . import common
from .common import FormatInfo

extensions = (".txt", ".text")

_PG_START = re.compile(r"^\W*\*{3}\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK\s*(?P<title>.*?)\s*\*{3}", re.I | re.M)
_PG_END = re.compile(r"^\W*\*{3}\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK", re.I | re.M)
_ILLUSTRATION = re.compile(r"^\[(illustration|image|decoration)\b[^\]]*\]?$", re.I)
_MULTIBYTE = ("gb", "big5", "shift_jis", "euc", "cp932", "cp949", "cp950", "iso2022", "hz", "johab")


# ------------------------------------------------------------------ decoding

def decode_bytes(raw: bytes) -> tuple[str, str]:
    """(text, encoding). utf-8-sig, utf-8, then charset-normalizer, then cp1252, then latin-1."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
    except Exception:  # noqa: BLE001
        best = None
    if best is not None:
        enc = best.encoding.lower()
        # single-byte Western guesses are unreliable between each other (and a BOM-less utf-16 guess on Western text is
        # nearly always wrong): Windows-1252 is what such files are
        if enc.startswith(_MULTIBYTE) or enc in {"cp1251", "koi8_r", "koi8_u", "cp866", "iso8859_5", "cp1253", "iso8859_7",
                                                 "cp1255", "cp1256", "iso8859_8", "iso8859_6", "tis_620", "cp874"}:
            try:
                return raw.decode(best.encoding), best.encoding
            except UnicodeDecodeError:
                pass
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return raw.decode("latin-1"), "latin-1"


def read_text(path: Path) -> str:
    text, _ = decode_bytes(Path(path).read_bytes())
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n").replace("\x00", "")
    return text


# ------------------------------------------------------------------ Project Gutenberg

def split_gutenberg(text: str) -> tuple[str, dict[str, str], str]:
    """(body, header fields, marker title). Without markers the text is returned untouched."""
    m = _PG_START.search(text)
    if not m:
        return text, {}, ""
    header = text[:m.start()]
    body = text[m.end():]
    e = _PG_END.search(body)
    if e:
        body = body[:e.start()]
    fields: dict[str, str] = {}
    for key in ("Title", "Author", "Release date", "Language", "Translator", "Editor", "Illustrator"):
        mm = re.search(rf"^{key}:[ \t]*(.+(?:\n[ \t]+\S.*)*)", header, re.I | re.M)
        if mm:
            fields[key.lower()] = re.sub(r"\s+", " ", mm.group(1)).strip()
    return body, fields, m.group("title").strip()


def _gutenberg_author(a: str) -> str:
    a = re.sub(r"\s*\(.*?\)\s*", " ", a).strip()
    m = re.fullmatch(r"([^,]+),\s*([^,]+)", a)
    if m and len(m.group(1).split()) == 1:          # "Doyle, Arthur Conan"
        return f"{m.group(2).strip()} {m.group(1).strip()}"
    return a


def _gutenberg_title(t: str) -> str:
    t = re.split(r";\s*or,?\s", t, maxsplit=1, flags=re.I)[0]
    return t.strip()


# ------------------------------------------------------------------ extraction

def book_from_text(text: str, path: Path, max_chapters: int | None = None, source: str = "txt") -> Book:
    from .pdf import pick_title_author, segment, split_parts
    body, fields, marker_title = split_gutenberg(text)
    lines = [l.rstrip() for l in body.split("\n")]
    info = {}
    if fields.get("title") or marker_title:
        info["Title"] = _gutenberg_title(fields.get("title") or marker_title.title())
    if fields.get("author"):
        info["Author"] = _gutenberg_author(fields["author"])
    title, author = pick_title_author(info, "\n".join(lines[:80]), common.stem_of(path))

    sections, dropped, mode = segment([lines])
    chapters: list[Chapter] = []
    if mode == "parts":
        paras = [p for p in common.lines_to_paragraphs(lines) if not _ILLUSTRATION.match(p)]
        paras = [p for p in (normalize_text(t) for t in paras) if p]
        chapters = split_parts(paras)
        if len(chapters) == 1:
            chapters[0].title = display_title(title)
        for c in chapters:
            c.source = source
    else:
        for ttl, flat in sections:
            ttl = re.sub(r"\s+", " ", ttl.replace("_", "")).strip()          # Gutenberg italics markers
            paras = [p for p in common.lines_to_paragraphs([l for _, l in flat]) if not _ILLUSTRATION.match(p)]
            paras = [p for p in (normalize_text(t) for t in paras) if p]
            if sum(len(p.split()) for p in paras) < common.MIN_CHAPTER_WORDS:
                dropped.append((ttl, "almost no text"))
                continue
            reason = common.matter_reason(ttl, " ".join(paras))
            if reason:
                dropped.append((ttl, reason))
                continue
            chapters.append(Chapter(ttl or f"Section {len(chapters) + 1}", paras, source))
    if not chapters:
        raise ValueError("no readable text found in the file")
    if max_chapters:
        chapters = chapters[:max_chapters]
    return Book(title=title, author=author, chapters=chapters, dropped=dropped)


def can_handle(path) -> bool:
    return Path(path).suffix.lower() in extensions


def extract(path, opts=None) -> Book:
    return book_from_text(read_text(Path(path)), Path(path), getattr(opts, "max_chapters", None))


def formats() -> list[FormatInfo]:
    return [FormatInfo("Plain text", (".txt",), "built in (encoding detection, Project Gutenberg aware)")]
