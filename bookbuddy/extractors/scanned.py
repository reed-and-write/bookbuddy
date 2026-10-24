"""Shared path for page-image formats (image folders, zip, CBZ, CBR, DJVU): OCR every page, then the same cleanup as PDF."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .. import config
from ..book import Book
from ..llm_clean import Cleaner
from .common import FormatInfo

log = logging.getLogger("bookbuddy.scanned")


def extract_scanned(path: Path, opts, open_ctx=None) -> Book:
    from .. import ocr
    from ..ocr.postprocess import clean_ocr_pages
    from ..ocr.sources import open_source
    from . import pdf

    path = Path(path)
    lang = getattr(opts, "ocr_lang", None) or config.get_settings().ocr_lang
    on_event = getattr(opts, "on_event", None)
    try:
        with open_source(path) as ps:
            cover = ps.cover_jpeg()
            res = ocr.run_ocr(ps, lang, getattr(opts, "ocr_workers", None) or None,
                              backend=getattr(opts, "ocr_backend", None) or None, on_event=on_event)
    except ocr.OcrError as e:
        raise ValueError(str(e)) from e
    pages = clean_ocr_pages([p.text for p in sorted(res.pages, key=lambda p: p.index)], lang)
    if sum(pdf.nonspace_chars(p) for p in pages) < pdf.MIN_PAGE_CHARS:
        raise ValueError(f"OCR found no readable text in {path.name} (are the pages images of text, in language {lang!r}?)")
    cleaner = Cleaner.from_choice(getattr(opts, "llm", None) or "auto")
    if on_event is not None:
        cleaner.on_event = on_event
    book = pdf.build_book(path, pages, {}, [], cleaner, getattr(opts, "max_chapters", None), cover)
    for ch in book.chapters:
        ch.source = "ocr"
    return book


def ocr_format_info(name: str, exts: tuple[str, ...], how: str, extra_need: str = "", extra_ok: bool = True,
                    extra_note: str = "") -> FormatInfo:
    from ..ocr import describe
    d = describe()
    ok = d["tesseract"] and extra_ok
    notes = []
    if not d["tesseract"]:
        notes.append("install Tesseract (Arch: tesseract tesseract-data-eng)")
    if extra_note and not extra_ok:
        notes.append(extra_note)
    needs = "Tesseract" + (f" + {extra_need}" if extra_need else "")
    return FormatInfo(name, exts, f"{how}; OCR with Tesseract ({d['lang']})", needs, ok, "; ".join(notes))
