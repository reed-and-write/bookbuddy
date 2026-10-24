"""Shared pieces of the OCR layer: errors, the PageText record, tool discovery, language checks, worker counts."""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import config

CACHE_VERSION = "1"          # bump to invalidate cached OCR text
SEC_PER_PAGE_GUESS = 2.0     # CPU-seconds per full-size 300 dpi page (measured ~1.5 for Tesseract on one core); only the first estimate


class OcrError(RuntimeError):
    """OCR cannot run or failed; the message is written for the person running Book Buddy."""


@dataclass
class PageText:
    index: int                  # 0-based page number in the source
    text: str
    source: str = "ocr"         # ocr | cache | text (the page already had a text layer)
    seconds: float = 0.0


@dataclass
class OcrResult:
    pages: list[PageText]
    backend: str                # tesseract | ocrmypdf
    seconds: float = 0.0
    searchable_pdf: Path | None = None
    cached: int = 0             # pages served from the cache


# ------------------------------------------------------------------ tools

def which(name: str) -> str | None:
    return shutil.which(name)


def tesseract_path() -> str | None:
    return which("tesseract")


@functools.lru_cache(maxsize=4)
def _tesseract_info(exe: str) -> tuple[frozenset[str], str]:
    langs: set[str] = set()
    version = ""
    try:
        r = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=30)
        for line in (r.stdout or "").splitlines()[1:]:
            line = line.strip()
            if line and not line.startswith("List of"):
                langs.add(line)
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        m = re.search(r"tesseract\s+(\S+)", (v.stdout or v.stderr or ""))
        version = m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return frozenset(langs), version


def tesseract_langs() -> set[str]:
    exe = tesseract_path()
    return set(_tesseract_info(exe)[0]) if exe else set()


def tesseract_version() -> str:
    exe = tesseract_path()
    return _tesseract_info(exe)[1] if exe else ""


MISSING_TESSERACT = ("OCR needs Tesseract (free). Install it (Arch: tesseract tesseract-data-eng, Debian/Ubuntu: "
                     "tesseract-ocr, macOS: brew install tesseract), then try again.")


def require_tesseract() -> str:
    exe = tesseract_path()
    if not exe:
        raise OcrError(MISSING_TESSERACT)
    return exe


def check_language(lang: str) -> None:
    """Every code in `lang` ("eng" or "eng+deu") must have Tesseract language data installed."""
    have = tesseract_langs()
    missing = [c for c in (x.strip() for x in lang.split("+")) if c and c not in have]
    if missing:
        c = missing[0]
        raise OcrError(f"Tesseract language data for {c!r} is not installed. Install the package that provides it "
                       f"(Arch: tesseract-data-{c}, Debian/Ubuntu: tesseract-ocr-{c}, macOS: brew install tesseract-lang) "
                       f"or change [ocr] lang in config.toml.")


# ------------------------------------------------------------------ ocrmypdf

def ocrmypdf_launcher() -> list[str] | None:
    """How to start ocrmypdf: this interpreter's module when importable, else an `ocrmypdf` program on PATH."""
    import importlib.util
    import sys
    if importlib.util.find_spec("ocrmypdf") is not None:
        return [sys.executable, "-m", "ocrmypdf"]
    exe = which("ocrmypdf")
    return [exe] if exe else None


def ocrmypdf_missing() -> str:
    """'' when ocrmypdf can run, else what is missing."""
    if ocrmypdf_launcher() is None:
        return "ocrmypdf is not installed (pip install 'bookbuddy[ocr]')"
    gone = [n for n in ("gs", "qpdf") if which(n) is None]
    if gone:
        names = {"gs": "Ghostscript", "qpdf": "qpdf"}
        return "ocrmypdf also needs " + " and ".join(names[g] for g in gone)
    if tesseract_path() is None:
        return "ocrmypdf also needs Tesseract"
    return ""


# ------------------------------------------------------------------ settings

def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def resolve_workers(workers: int | None = None) -> int:
    n = workers if workers else config.get_settings().ocr_workers
    return max(1, int(n)) if n else default_workers()


def estimate_seconds(pages: int, workers: int, rate: float | None = None) -> float:
    """Wall-clock guess for OCRing `pages` pages with `workers` parallel Tesseract processes."""
    return pages * (rate if rate else SEC_PER_PAGE_GUESS) / max(1, workers)


def describe() -> dict:
    """What `bookbuddy formats` prints about OCR."""
    st = config.get_settings()
    t = tesseract_path()
    om = ocrmypdf_missing()
    pdf_backend = "none"
    if st.ocr_backend == "tesseract":
        pdf_backend = "tesseract" if t else "none"
    elif st.ocr_backend == "ocrmypdf":
        pdf_backend = "ocrmypdf" if not om else "none"
    else:
        pdf_backend = "ocrmypdf" if not om else ("tesseract" if t else "none")
    return {"setting": st.ocr_backend, "pdf_backend": pdf_backend, "image_backend": "tesseract" if t else "none",
            "lang": st.ocr_lang, "workers": resolve_workers(), "tesseract": bool(t), "tesseract_version": tesseract_version(),
            "ocrmypdf_problem": om, "languages": sorted(tesseract_langs())}
