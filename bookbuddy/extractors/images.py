"""A folder of page images, or a .zip of them -> OCR -> Book. Pages are read in natural file-name order."""
from __future__ import annotations

import zipfile
from pathlib import Path

from ..book import Book
from . import scanned
from ..ocr import sources

extensions = (".zip",)


def can_handle(path) -> bool:
    p = Path(path)
    if p.is_dir():
        try:
            return bool(sources.list_images(p))
        except OSError:
            return False
    if p.suffix.lower() != ".zip" or p.name.lower().endswith(".fb2.zip"):
        return False
    try:
        with zipfile.ZipFile(p) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, OSError):
        return False
    imgs = [n for n in names if sources.is_image_name(n)]
    return bool(imgs) and len(imgs) >= 0.5 * len(names)


def extract(path, opts=None) -> Book:
    return scanned.extract_scanned(Path(path), opts)


def formats():
    return [scanned.ocr_format_info("Image folder / ZIP", (".zip", "folder"), "page images in natural order")]
