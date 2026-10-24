"""OCR layer. One interface, two backends.

    ocr_pages(source, lang=None, workers=None, ...) -> list[PageText]

`source` is a PDF, a DJVU, an image folder, a zip/CBZ/CBR, or a list of image files. Backends:

    ocrmypdf   PDF input only. Cleans the page images (deskew, optional unpaper) and writes a searchable PDF, which the
               normal PDF extractor then reads. Used by "auto" when ocrmypdf, Ghostscript, qpdf and Tesseract are present.
    tesseract  always available when Tesseract is installed, and the only path for images / CBZ / CBR / DJVU. Renders
               pages (pdftoppm, ddjvu, archive extraction) and runs tesseract on them in a bounded pool on the CPU.

Progress is reported through `on_event(kind, **info)`: 'ocr_plan' once, 'ocr_progress' every few pages.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from .. import config
from .common import (OcrError, OcrResult, PageText, check_language, describe, default_workers, ocrmypdf_missing,
                     require_tesseract, resolve_workers, tesseract_path)
from .sources import PageSource, open_source

__all__ = ["OcrError", "OcrResult", "PageText", "describe", "ocr_pages", "run_ocr", "select_backend", "ocr_source_pages"]

log = logging.getLogger("bookbuddy.ocr")


def select_backend(setting: str | None = None, is_pdf: bool = True) -> str:
    """'ocrmypdf' or 'tesseract' for this input. Forced-but-missing backends raise a clear OcrError."""
    setting = setting or config.get_settings().ocr_backend
    if setting not in config.OCR_BACKENDS:
        raise OcrError(f"unknown OCR backend {setting!r} (use one of {', '.join(config.OCR_BACKENDS)})")
    if setting == "tesseract":
        require_tesseract()
        return "tesseract"
    problem = ocrmypdf_missing()
    if setting == "ocrmypdf":
        if problem:
            raise OcrError(f"OCR backend 'ocrmypdf' was requested but {problem}.")
        if not is_pdf:
            log.info("ocrmypdf reads PDFs only; using tesseract for this input")
            require_tesseract()
            return "tesseract"
        return "ocrmypdf"
    # auto
    if is_pdf and not problem:
        return "ocrmypdf"
    require_tesseract()
    return "tesseract"


def _emitter(on_event: Callable | None):
    if on_event is None:
        return None

    def emit(kind: str, **info) -> None:
        try:
            on_event(kind, **info)
        except Exception:  # noqa: BLE001 - notifications must never fail a job
            log.exception("OCR progress hook failed")
    return emit


def ocr_source_pages(source: PageSource, indices: list[int], lang: str, workers: int, on_event=None) -> OcrResult:
    from .tesseract import ocr_source
    t0 = time.monotonic()
    check_language(lang)
    pages, cached = ocr_source(source, indices, lang, workers, _emitter(on_event))
    return OcrResult(pages, "tesseract", time.monotonic() - t0, None, cached)


def run_ocr(source, lang: str | None = None, workers: int | None = None, *, backend: str | None = None,
            pages: list[int] | None = None, on_event: Callable | None = None) -> OcrResult:
    """OCR `source` (all pages, or only the 0-based indices in `pages`)."""
    st = config.get_settings()
    lang = lang or st.ocr_lang
    workers = resolve_workers(workers)
    is_pdf = (not isinstance(source, (list, tuple, PageSource))) and Path(source).suffix.lower() == ".pdf" and not Path(source).is_dir()
    chosen = select_backend(backend, is_pdf)
    if chosen == "ocrmypdf":
        from .ocrmypdf_backend import make_searchable
        from ..extractors.pdf import pdf_pages
        src = Path(source)
        t0 = time.monotonic()
        with open_source(src) as ps:
            total = ps.count
        try:
            out, reused = make_searchable(src, lang, workers, pages, total, _emitter(on_event), st.ocr_progress_every)
        except OcrError as e:
            if backend == "ocrmypdf" or st.ocr_backend == "ocrmypdf":
                raise
            log.warning("ocrmypdf failed (%s); falling back to tesseract", e)
            chosen = "tesseract"
        else:
            # Tesseract's own page text (sidecar) keeps its paragraph/line structure; poppler re-deriving it from the
            # invisible text layer splits OCR lines into one-word blocks. The searchable PDF is still kept.
            from .ocrmypdf_backend import read_sidecar
            texts = read_sidecar(out, total)
            if texts is None:
                texts = pdf_pages(out)
            idx = pages if pages is not None else list(range(len(texts)))
            return OcrResult([PageText(i, texts[i] if i < len(texts) else "", "cache" if reused else "ocr") for i in idx],
                             "ocrmypdf", time.monotonic() - t0, out, len(idx) if reused else 0)
    if isinstance(source, PageSource):
        idx = pages if pages is not None else list(range(source.count))
        return ocr_source_pages(source, idx, lang, workers, on_event)
    with open_source(source) as ps:
        idx = pages if pages is not None else list(range(ps.count))
        return ocr_source_pages(ps, idx, lang, workers, on_event)


def ocr_pages(source, lang: str | None = None, workers: int | None = None, *, backend: str | None = None,
              pages: list[int] | None = None, on_event: Callable | None = None) -> list[PageText]:
    """The public interface: OCR a PDF / DJVU / image folder / archive / list of images into per-page text."""
    return run_ocr(source, lang, workers, backend=backend, pages=pages, on_event=on_event).pages
