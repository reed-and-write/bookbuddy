"""RTF: striprtf turns it into plain text, then the TXT path takes over."""
from __future__ import annotations

import re
from pathlib import Path

from ..book import Book
from . import common, txt
from .common import FormatInfo

extensions = (".rtf",)


def _unescape(s: str) -> str:
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), s)
    s = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), s)
    return s


def _info_field(raw: str, name: str) -> str:
    """\\title / \\author from the {\\info ...} group (also inside LibreOffice's {\\upr{...}} wrappers)."""
    start = raw.find("{\\info")
    if start < 0:
        return ""
    m = re.search(r"\\" + name + r"\s+((?:[^{}\\]|\\'[0-9a-fA-F]{2}|\\u-?\d+\??)*)\}", raw[start:start + 4000])
    return common.clean_meta(_unescape(m.group(1))) if m else ""


def can_handle(path) -> bool:
    return Path(path).suffix.lower() == ".rtf"


def extract(path, opts=None) -> Book:
    from striprtf.striprtf import rtf_to_text
    path = Path(path)
    raw = path.read_bytes().decode("latin-1")      # RTF is 7-bit ASCII with escapes
    if not raw.lstrip().startswith("{\\rtf"):
        raise ValueError("this does not look like an RTF file")
    text = rtf_to_text(raw, errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
    book = txt.book_from_text(text, path, getattr(opts, "max_chapters", None), source="rtf")
    t, a = _info_field(raw, "title"), _info_field(raw, "author")
    if t and not common._JUNK_TITLE.match(t):
        book.title = t
    if a and not common._JUNK_AUTHOR.match(a):
        book.author = a
    return book


def formats() -> list[FormatInfo]:
    return [FormatInfo("RTF", (".rtf",), "striprtf, then the plain-text path")]
