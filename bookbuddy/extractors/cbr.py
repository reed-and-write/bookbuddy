"""Comic book archives (.cbr = RAR of page images) -> OCR -> Book. Needs unrar, 7-zip or bsdtar for real RAR files."""
from __future__ import annotations

from pathlib import Path

from ..book import Book
from ..ocr import sources
from . import scanned

extensions = (".cbr",)


def can_handle(path) -> bool:
    p = Path(path)
    return p.suffix.lower() == ".cbr" and p.is_file()


def extract(path, opts=None) -> Book:
    return scanned.extract_scanned(Path(path), opts)


def formats():
    ok = sources.rar_tool() is not None
    return [scanned.ocr_format_info("CBR", (".cbr",), "RAR of page images", "unrar", ok, sources.NEEDS_UNRAR)]
