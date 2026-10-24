"""ocrmypdf backend (PDF only): image clean-up + OCR into a searchable PDF that the normal PDF extractor then reads."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .common import (CACHE_VERSION, OcrError, check_language, default_workers, estimate_seconds, ocrmypdf_launcher,
                     tesseract_version, which)
from .tesseract import cache_root

log = logging.getLogger("bookbuddy.ocr")
PLUGIN = Path(__file__).with_name("_progress_plugin.py")
OPTIONS_VERSION = "1"


def page_spec(pages: list[int]) -> str:
    """0-based page indices -> ocrmypdf's 1-based '1,3-5' form."""
    nums = sorted({p + 1 for p in pages})
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(out)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sidecar_for(pdf: Path) -> Path:
    return pdf.with_suffix(".txt")


def read_sidecar(pdf: Path, n_pages: int) -> list[str] | None:
    """Tesseract's own page text saved next to the searchable PDF (form-feed separated), or None."""
    f = sidecar_for(pdf)
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return None
    pages = text.split("\f")
    if len(pages) < n_pages - 1 or len(pages) > n_pages + 1:
        return None                                   # not the file we expect: let the caller read the PDF instead
    return (pages + [""] * n_pages)[:n_pages]          # a blank last page leaves a short tail


def build_command(src: Path, dst: Path, lang: str, workers: int, pages: list[int] | None) -> list[str]:
    launcher = ocrmypdf_launcher()
    if launcher is None:
        raise OcrError("ocrmypdf is not installed")
    cmd = launcher + ["-l", lang, "--jobs", str(workers), "--skip-text", "--deskew", "--output-type", "pdf",
                      "--sidecar", str(sidecar_for(dst)), "--plugin", str(PLUGIN)]
    if which("unpaper"):
        cmd.append("--clean")
    if pages is not None:
        cmd += ["--pages", page_spec(pages)]
    return cmd + [str(src), str(dst)]


def searchable_path(src: Path, lang: str, pages: list[int] | None) -> Path:
    key = hashlib.sha256(f"{file_sha256(src)}|{lang}|{page_spec(pages) if pages is not None else 'all'}|"
                         f"tess{tesseract_version().split('.')[0]}|o{OPTIONS_VERSION}|v{CACHE_VERSION}".encode()).hexdigest()[:40]
    return cache_root() / "pdf" / f"{key}.pdf"


def make_searchable(src: Path, lang: str, workers: int, pages: list[int] | None, total_pages: int,
                    emit: Callable[..., None] | None = None, progress_every: int = 10) -> tuple[Path, bool]:
    """Run ocrmypdf. Returns (searchable pdf path inside the cache, True if it was already there)."""
    check_language(lang)
    out = searchable_path(src, lang, pages)
    n = len(pages) if pages is not None else total_pages
    workers = max(1, workers or default_workers())
    if out.exists() and out.stat().st_size > 0:
        if emit:
            emit("ocr_plan", pages=n, total_pages=total_pages, backend="ocrmypdf", workers=workers, est_seconds=0.0,
                 lang=lang, cached=True)
            emit("ocr_progress", done=n, pages=n, percent=100, remaining_seconds=0.0, pages_per_second=0.0, backend="ocrmypdf")
        log.info("ocr (ocrmypdf): reusing searchable PDF %s", out.name)
        return out, True
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:8]}.part.pdf")
    cmd = build_command(src, tmp, lang, workers, pages)
    if emit:
        emit("ocr_plan", pages=n, total_pages=total_pages, backend="ocrmypdf", workers=workers,
             est_seconds=estimate_seconds(n, workers), lang=lang)
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace",
                            env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    tail: list[str] = []
    last_emitted = 0

    def pump() -> None:
        nonlocal last_emitted
        assert proc.stderr is not None
        for line in proc.stderr:
            m = re.match(r"BBOCR (\d+) (\d+)", line)
            if not m:
                tail.append(line.rstrip())
                del tail[:-30]
                continue
            done, tot = int(m.group(1)), int(m.group(2))
            if emit and (done % progress_every == 0 or done == tot) and done != last_emitted:
                last_emitted = done
                el = time.monotonic() - t0
                rate = done / el if el > 0 else 0.0
                emit("ocr_progress", done=done, pages=tot, percent=int(100 * done / tot) if tot else 100,
                     remaining_seconds=(tot - done) / rate if rate and done < tot else 0.0, pages_per_second=rate,
                     backend="ocrmypdf")

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    try:
        rc = proc.wait(timeout=6 * 3600)
    except BaseException:
        proc.kill()
        raise
    th.join(timeout=5)
    if rc != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        sidecar_for(tmp).unlink(missing_ok=True)
        msg = " | ".join(t for t in tail if t.strip())[-400:]
        raise OcrError(f"ocrmypdf failed (exit {rc}): {msg}")
    side = sidecar_for(tmp)
    if side.exists():
        os.replace(side, sidecar_for(out))
    os.replace(tmp, out)
    el = time.monotonic() - t0
    log.info("ocr (ocrmypdf): %d pages in %.1fs = %.2f pages/s with %d jobs", n, el, n / el if el else 0, workers)
    return out, False
