"""Calibre-backed formats: MOBI, AZW, AZW3, KFX, LIT, PDB, LRF, SNB, TCR, CBC ... via `ebook-convert`.

Calibre is GPL, so it is only ever run as an external program, never imported. The converted EPUB is cached by the
SHA-256 of the source file and handed to the EPUB extractor. Without Calibre these formats fail with a clear message.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config
from ..book import Book
from . import drm
from .common import FormatInfo

log = logging.getLogger("bookbuddy.calibre")

extensions = (".mobi", ".azw", ".azw3", ".azw4", ".kfx", ".prc", ".lit", ".pdb", ".lrf", ".snb", ".tcr", ".cbc", ".pml", ".rb")
KINDLE = (".mobi", ".azw", ".azw3", ".azw4", ".kfx", ".prc")
MISSING_MESSAGE = "This format needs Calibre (free). Install it from calibre-ebook.com, then try again."
CONVERT_TIMEOUT = 1800

_EXTRA_LOCATIONS = ("/Applications/calibre.app/Contents/MacOS/ebook-convert", "/opt/calibre/ebook-convert",
                    "C:/Program Files/Calibre2/ebook-convert.exe")


class CalibreMissing(RuntimeError):
    def __init__(self):
        super().__init__(MISSING_MESSAGE)


def find_ebook_convert() -> str | None:
    found = shutil.which("ebook-convert")
    if found:
        return found
    for p in _EXTRA_LOCATIONS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def converted_epub(src: Path) -> Path:
    """The EPUB Calibre makes from `src`, converting only when it is not already cached."""
    src = Path(src)
    if src.suffix.lower() in KINDLE:
        drm.check_kindle(src)
    exe = find_ebook_convert()
    if exe is None:
        raise CalibreMissing()
    cache = config.get_settings().cache_dir / "calibre"
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{file_sha256(src)}.epub"
    if dest.exists() and dest.stat().st_size > 0:
        log.info("calibre: using cached conversion %s", dest.name)
        return dest
    with tempfile.TemporaryDirectory(dir=cache, prefix="convert-") as td:
        out = Path(td) / "book.epub"
        tmp_src = Path(td) / f"source{src.suffix.lower()}"
        try:
            os.link(src, tmp_src)
        except OSError:
            shutil.copyfile(src, tmp_src)                # ebook-convert guesses the input type from the extension
        log.info("calibre: converting %s", src.name)
        try:
            r = subprocess.run([exe, str(tmp_src), str(out)], capture_output=True, text=True, timeout=CONVERT_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Calibre took longer than {CONVERT_TIMEOUT // 60} minutes to convert {src.name}") from e
        text = (r.stdout or "") + "\n" + (r.stderr or "")
        if r.returncode != 0 or not out.exists():
            if drm.looks_like_drm_failure(text):
                raise drm.DRMError("Calibre reported DRM")
            tail = " ".join(text.strip().splitlines()[-3:])[-300:]
            raise RuntimeError(f"Calibre could not convert {src.name}: {tail}")
        os.replace(out, dest)
    return dest


def can_handle(path) -> bool:
    return Path(path).suffix.lower() in extensions


def extract(path, opts=None) -> Book:
    from . import epub
    path = Path(path)
    cached = converted_epub(path)
    book = epub.extract(cached, opts)
    if book.title == cached.stem:                  # the converted EPUB had no title: use the source file's name
        from .common import resolve_title_author
        book.title, author = resolve_title_author(path, "", "")
        if book.author == "Unknown":
            book.author = author
    return book


def formats() -> list[FormatInfo]:
    ok = find_ebook_convert() is not None
    note = "" if ok else "Calibre is not installed (calibre-ebook.com, free)"
    return [FormatInfo("Kindle (MOBI, AZW, AZW3, KFX)", (".mobi", ".azw", ".azw3", ".kfx"), "Calibre ebook-convert", "Calibre",
                       ok, note or "DRM-protected books are refused; KFX also needs Calibre's KFX Input plugin"),
            FormatInfo("Other e-book formats", (".lit", ".pdb", ".lrf", ".snb", ".tcr", ".cbc"), "Calibre ebook-convert",
                       "Calibre", ok, note)]
