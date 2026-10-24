"""Tesseract backend: render each page, OCR it with the tesseract program, bounded parallelism on CPU, per-page cache."""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from .. import config
from .common import CACHE_VERSION, OcrError, PageText, estimate_seconds, require_tesseract, tesseract_version
from .sources import PageSource

log = logging.getLogger("bookbuddy.ocr")
PAGE_TIMEOUT = 600


def cache_root() -> Path:
    return config.get_settings().cache_dir / "ocr"


def page_key(image: Path, lang: str) -> str:
    h = hashlib.sha256()
    with Path(image).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    h.update(f"|{lang}|tess{tesseract_version().split('.')[0]}|v{CACHE_VERSION}".encode())
    return h.hexdigest()[:40]


def _cache_file(key: str) -> Path:
    return cache_root() / "pages" / key[:2] / f"{key}.txt"


def cache_get(key: str) -> str | None:
    try:
        return _cache_file(key).read_text(encoding="utf-8")
    except OSError:
        return None


def cache_put(key: str, text: str) -> None:
    f = _cache_file(key)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f"{f.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        log.warning("ocr cache write failed: %s", e)


def run_tesseract(image: Path, lang: str, exe: str) -> str:
    env = dict(os.environ, OMP_THREAD_LIMIT="1")        # parallelism comes from the page pool, not OpenMP
    r = subprocess.run([exe, str(image), "stdout", "-l", lang, "--psm", "3"], capture_output=True, env=env, timeout=PAGE_TIMEOUT)
    if r.returncode != 0:
        raise OcrError(f"tesseract failed on {Path(image).name}: {r.stderr.decode('utf-8', 'replace')[-300:]}")
    return r.stdout.decode("utf-8", "replace")


def ocr_source(source: PageSource, indices: list[int], lang: str, workers: int,
               emit: Callable[..., None] | None = None, progress_every: int | None = None) -> tuple[list[PageText], int]:
    """OCR the pages `indices` of `source`. Returns (PageText in `indices` order, pages served from cache)."""
    exe = require_tesseract()
    total = len(indices)
    every = max(1, progress_every or config.get_settings().ocr_progress_every)
    workers = max(1, min(workers, total or 1))
    results: dict[int, PageText] = {}
    cached = 0
    t0 = time.monotonic()
    ocr_secs = 0.0
    ocr_pages_done = 0

    def work(i: int) -> PageText:
        t = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="bookbuddy-ocr-") as td:
            img = source.render(i, Path(td))
            key = page_key(img, lang)
            hit = cache_get(key)
            if hit is not None:
                return PageText(i, hit, "cache", time.monotonic() - t)
            text = run_tesseract(img, lang, exe)
        cache_put(key, text)
        return PageText(i, text, "ocr", time.monotonic() - t)

    if emit:
        emit("ocr_plan", pages=total, total_pages=source.count, backend="tesseract", workers=workers,
             est_seconds=estimate_seconds(total, workers), lang=lang)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, i): i for i in indices}
        done = 0
        try:
            for fut in as_completed(futs):
                pt = fut.result()
                results[pt.index] = pt
                done += 1
                if pt.source == "cache":
                    cached += 1
                else:
                    ocr_secs += pt.seconds
                    ocr_pages_done += 1
                if emit and (done % every == 0 or done == total):
                    el = time.monotonic() - t0
                    rate = done / el if el > 0 else 0.0
                    remaining = (total - done) / rate if rate and done < total else 0.0
                    emit("ocr_progress", done=done, pages=total, percent=int(100 * done / total),
                         remaining_seconds=remaining, pages_per_second=rate, backend="tesseract")
        except BaseException:
            for f in futs:
                f.cancel()
            raise
    el = time.monotonic() - t0
    if total:
        log.info("ocr (tesseract): %d pages in %.1fs = %.2f pages/s with %d workers (%d from cache)",
                 total, el, total / el if el else 0, workers, cached)
    return [results[i] for i in indices], cached
