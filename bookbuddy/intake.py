"""Putting a book into the library queue: safe file names, staging a copy in the data dir, one enqueue path for
the web API, the watcher and the CLI."""
from __future__ import annotations

import hashlib
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import BinaryIO

from . import config
from .extractors import is_supported_name, supported_extensions
from .library import Library


class IntakeError(ValueError):
    pass


class TooLarge(IntakeError):
    pass


def safe_filename(name: str) -> str:
    """A plain file name: no directories, no control characters, no leading dots, bounded length. The extension survives."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:100] + dot + ext[:15]) if dot else name[:120]
    return name or "book"


def uploads_dir() -> Path:
    return config.data_dir() / "uploads"


def stage_stream(stream: BinaryIO, filename: str, max_bytes: int) -> tuple[Path, str, int]:
    """Copy an upload into uploads/<random>/<safe name>, hashing as we go. Raises IntakeError (bad type) or TooLarge."""
    name = safe_filename(filename)
    if not is_supported_name(name):
        raise IntakeError(f"unsupported file type {Path(name).suffix.lower()!r} (supported: {', '.join(sorted(supported_extensions()))})")
    folder = uploads_dir() / uuid.uuid4().hex
    folder.mkdir(parents=True)
    dest = folder / name
    h, size = hashlib.sha256(), 0
    try:
        with open(dest, "wb") as out:
            while block := stream.read(1 << 20):
                size += len(block)
                if size > max_bytes:
                    raise TooLarge(f"file is larger than the {max_bytes // (1 << 20)} MB upload limit")
                h.update(block)
                out.write(block)
        if size == 0:
            raise IntakeError("the file is empty")
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return dest, h.hexdigest(), size


def stage_path(path: Path) -> tuple[Path, str, int]:
    """Copy a local file (watcher / CLI) into uploads/. The original is left where it is."""
    path = Path(path)
    if path.is_dir():
        raise IntakeError("folders can only be converted in the foreground (bookbuddy listen --now)")
    with open(path, "rb") as f:
        return stage_stream(f, path.name, max_bytes=1 << 42)


def enqueue_file(lib: Library, staged: Path, sha: str, *, origin: str, voice: str, speed: float, fmt: str,
                 llm: str | None = None, extra: dict | None = None, original_name: str | None = None) -> tuple[int, int]:
    """Create the book + queued job for a staged file. `llm`: None = settings, 'auto', 'none' or a provider name."""
    name = original_name or staged.name
    title = re.sub(r"[_]+", " ", Path(name).stem).strip() or name
    options = {"llm": llm, **(extra or {})}
    return lib.enqueue(title=title, source_name=name, source_hash=sha, source_path=str(staged), voice=voice, speed=speed,
                       fmt=fmt, origin=origin, options=options)
