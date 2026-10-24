"""Comic book archives (.cbz = zip of page images) -> OCR -> Book."""
from __future__ import annotations

from pathlib import Path

from ..book import Book
from . import scanned
from ..ocr import sources

extensions = (".cbz",)


def can_handle(path) -> bool:
    p = Path(path)
    return p.suffix.lower() == ".cbz" and p.is_file()


def extract(path, opts=None) -> Book:
    return scanned.extract_scanned(Path(path), opts)


def formats():
    return [scanned.ocr_format_info("CBZ", (".cbz",), "zip of page images")]
