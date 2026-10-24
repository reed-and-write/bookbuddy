"""Inspect an uploaded book before it is queued: cheap metadata only (no OCR, no TTS, no LLM).

`POST /api/inspect` stages the file as `uploads/<upload id>/<name>` plus a hidden `.inspect.json` next to it. The web page
shows the result in the "Add a book" panel; "Make audiobook" then queues the staged file (`POST /api/books` with
`upload_id`) without a second upload, and the cross discards it. Anything still unclaimed after 24 hours is removed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from . import intake

log = logging.getLogger("bookbuddy.preview")

META_NAME = ".inspect.json"
EXPIRY_SECONDS = 24 * 3600
ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Audio length estimate: Kokoro read the 26,552-word test book in 9,344 s at speed 1.0, about 170 words a minute.
# Playing time scales inversely with the chosen speed.
WORDS_PER_MINUTE = 170

_lock = threading.Lock()


class UploadGone(LookupError):
    """The upload id is unknown (never existed, discarded, or already queued)."""


class UploadExpired(UploadGone):
    """The staged file was older than the 24 hour limit and has been removed."""


def estimate_seconds(words: int, speed: float = 1.0) -> int:
    return int(round(words / WORDS_PER_MINUTE * 60 / max(0.1, speed)))


def _folder(upload_id: str) -> Path:
    if not ID_RE.match(upload_id or ""):
        raise UploadGone("no such upload")
    return intake.uploads_dir() / upload_id


def _format_name(path: Path) -> str:
    from .extractors import list_formats
    name = path.name.lower()
    for row in list_formats():
        if any(name.endswith(e) for e in row.extensions):
            return row.name
    return path.suffix.lstrip(".").upper()


def _needs_ocr(path: Path) -> bool:
    """True when reading this file means running OCR (pages are pictures), so chapters are unknown until it is read."""
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in (".cbz", ".cbr") or (suffix == ".zip" and not name.endswith(".fb2.zip")):
        return True
    if suffix in (".djvu", ".djv"):
        from .extractors import djvu
        return djvu.text_layer(path) is None
    if suffix == ".pdf":
        from .extractors import pdf
        pages = pdf.pdf_pages(path)
        return bool(pages) and bool(pdf.ocr_candidates(path, pages))
    return False


def inspect_file(path: Path) -> dict:
    """Metadata for a staged file. Scanned formats return only what is cheap (title from the file name)."""
    from .extractors import extract_book
    path = Path(path)
    out = {"format": _format_name(path), "title": None, "author": None, "chapters": None, "words": None,
           "has_cover": False, "needs_ocr": False, "_cover": None, "_ext": "jpg"}
    if _needs_ocr(path):
        from .extractors.common import resolve_title_author
        out["needs_ocr"] = True
        out["title"], author = resolve_title_author(path, "", "")
        out["author"] = author if author != "Unknown" else None
        return out
    book = extract_book(path, SimpleNamespace(llm="none", on_event=None, max_chapters=None))
    out.update(title=book.title, author=(book.author if book.author != "Unknown" else None), chapters=len(book.chapters),
               words=book.words, has_cover=bool(book.cover), _cover=book.cover, _ext=book.cover_ext or "jpg")
    return out


def _public(upload_id: str, meta: dict, speed: float | None = None) -> dict:
    words = meta.get("words")
    return {"upload_id": upload_id, "filename": meta["filename"], "size": meta["size"], "format": meta["format"],
            "title": meta["title"], "author": meta["author"], "chapters": meta["chapters"], "words": words,
            "audio_seconds": estimate_seconds(words, speed or meta.get("speed") or 1.0) if words else None,
            "words_per_minute": WORDS_PER_MINUTE, "has_cover": meta["has_cover"], "needs_ocr": meta["needs_ocr"]}


def inspect_upload(staged: Path, sha: str, size: int, speed: float) -> dict:
    """Inspect a freshly staged upload, write its sidecar, return the API view. Removes the upload if it cannot be read."""
    folder = staged.parent
    upload_id = folder.name
    try:
        info = inspect_file(staged)
        cover = info.pop("_cover")
        ext = info.pop("_ext")
        if cover:
            (folder / f".cover.{ext}").write_bytes(cover)
        meta = {**info, "filename": staged.name, "size": size, "sha": sha, "created": time.time(),
                "cover_file": f".cover.{ext}" if cover else None, "speed": speed}
        tmp = folder / (META_NAME + ".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, folder / META_NAME)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return _public(upload_id, meta, speed)


def load_meta(upload_id: str) -> tuple[Path, dict]:
    folder = _folder(upload_id)
    mp = folder / META_NAME
    try:
        meta = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UploadGone("no such upload") from None
    if time.time() - meta.get("created", 0) > EXPIRY_SECONDS:
        shutil.rmtree(folder, ignore_errors=True)
        raise UploadExpired("this upload expired; choose the file again")
    return folder, meta


def cover_path(upload_id: str) -> Path | None:
    folder, meta = load_meta(upload_id)
    cf = meta.get("cover_file")
    p = folder / cf if cf else None
    return p if p is not None and p.is_file() else None


def discard(upload_id: str) -> None:
    with _lock:
        folder, _ = load_meta(upload_id)
        shutil.rmtree(folder, ignore_errors=True)


def claim(upload_id: str) -> tuple[Path, dict]:
    """Take the staged file for queueing, exactly once: the sidecar is removed, so a second claim finds nothing."""
    with _lock:
        folder, meta = load_meta(upload_id)
        staged = folder / meta["filename"]
        if not staged.is_file():
            shutil.rmtree(folder, ignore_errors=True)
            raise UploadGone("no such upload")
        (folder / META_NAME).unlink(missing_ok=True)
        cf = meta.get("cover_file")
        if cf:
            (folder / cf).unlink(missing_ok=True)
        return staged, meta


def cleanup_expired(now: float | None = None) -> int:
    """Remove unclaimed inspected uploads older than 24 h (called on server start and periodically)."""
    now = time.time() if now is None else now
    root = intake.uploads_dir()
    removed = 0
    if not root.is_dir():
        return 0
    with _lock:
        for folder in root.iterdir():
            mp = folder / META_NAME
            if not folder.is_dir() or not mp.is_file():
                continue
            try:
                created = json.loads(mp.read_text(encoding="utf-8")).get("created", 0)
            except (OSError, ValueError):
                created = mp.stat().st_mtime
            if now - created > EXPIRY_SECONDS:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    if removed:
        log.info("removed %d expired inspected upload(s)", removed)
    return removed
