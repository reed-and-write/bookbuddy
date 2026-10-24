"""DjVu: use the file's own text layer when it has one (fast, no OCR); otherwise ddjvu page renders -> OCR -> Book."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from ..book import Book
from . import scanned

log = logging.getLogger("bookbuddy.djvu")

extensions = (".djvu", ".djv")
MIN_REAL_PAGE_CHARS = 40          # a page with less text than this has no usable text layer
MIN_PAGES_WITH_TEXT = 0.5         # share of pages that must have real text


def can_handle(path) -> bool:
    p = Path(path)
    return p.suffix.lower() in extensions and p.is_file()


def text_layer(path: Path) -> list[str] | None:
    """Per-page text from `djvutxt` when the file carries a real text layer, else None (OCR is needed)."""
    if shutil.which("djvutxt") is None:
        return None
    try:
        r = subprocess.run(["djvutxt", str(path)], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("djvutxt failed on %s: %s", path.name, e)
        return None
    if r.returncode != 0:
        return None
    pages = r.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()                                   # djvutxt ends the last page with a form feed
    if not pages:
        return None
    from .pdf import nonspace_chars
    real = sum(1 for p in pages if nonspace_chars(p) >= MIN_REAL_PAGE_CHARS)
    return pages if real / len(pages) >= MIN_PAGES_WITH_TEXT else None


def extract(path, opts=None) -> Book:
    path = Path(path)
    pages = text_layer(path)
    if pages is None:
        return scanned.extract_scanned(path, opts)
    from .. import config
    from ..llm_clean import Cleaner
    from ..ocr.postprocess import clean_ocr_pages
    from ..ocr.sources import open_source
    from . import pdf

    log.info("%s has a text layer (%d pages): skipping OCR", path.name, len(pages))
    lang = getattr(opts, "ocr_lang", None) or config.get_settings().ocr_lang
    try:
        with open_source(path) as ps:
            cover = ps.cover_jpeg()
    except Exception:  # noqa: BLE001 - the cover is optional
        cover = None
    pages = clean_ocr_pages(pages, lang)
    cleaner = Cleaner.from_choice(getattr(opts, "llm", None) or "auto")
    if getattr(opts, "on_event", None) is not None:
        cleaner.on_event = opts.on_event
    book = pdf.build_book(path, pages, {}, [], cleaner, getattr(opts, "max_chapters", None), cover)
    for ch in book.chapters:
        ch.source = "djvu-text"
    return book


def formats():
    ok = shutil.which("ddjvu") is not None and shutil.which("djvused") is not None
    return [scanned.ocr_format_info("DjVu", extensions, "text layer when present (djvutxt), else DjVuLibre ddjvu page renders",
                                    "DjVuLibre", ok, "install DjVuLibre (Arch: djvulibre)")]
