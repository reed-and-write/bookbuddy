"""Run one inbox file to completion and move the source (never delete it)."""
from __future__ import annotations

import logging
import shutil
import dataclasses
import time
import traceback
from pathlib import Path

from .notify import Notifier
from .pipeline import Options, convert

log = logging.getLogger("bookbuddy.jobs")


def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")


def move_source(src: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(folder / src.name)
    shutil.move(str(src), str(dest))
    return dest


def notifier_hook(notifier: Notifier):
    """An Options.on_event hook that turns pipeline events into notifier messages."""
    def on_event(kind: str, **info) -> None:
        if kind == "planned":
            notifier.planned(info["title"], info["author"], info["chapters"], info["est_seconds"],
                             info["resumed"], info["chunks"])
        elif kind == "llm_plan":
            notifier.llm_plan(info["to_send"], info["chunks"], info["est_seconds"], info["provider"])
        elif kind == "ocr_plan":
            notifier.ocr_plan(info["pages"], info["total_pages"], info["est_seconds"], info["backend"], info.get("cached", False))
        elif kind == "ocr_progress":
            notifier.ocr_progress(info["done"], info["pages"], info["percent"], info.get("remaining_seconds"))
        elif kind == "progress":
            notifier.progress(info["title"], info["percent"], info["remaining_seconds"])
    return on_event


def write_failure_note(src: Path, inbox: Path, reason: str, tb: str = "") -> None:
    """Move a failed source to inbox/failed/ with a <name>.error.txt beside it."""
    dest = move_source(src, inbox / "failed")
    note = dest.with_name(dest.name + ".error.txt")
    note.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\nFile: {src.name}\nReason: {reason}\n\n{tb}", encoding="utf-8")


def process_file(src: Path, inbox: Path, opts: Options, notifier: Notifier, convert_fn=convert) -> bool:
    """Returns True on success. Success -> inbox/done/, failure -> inbox/failed/ + <name>.error.txt."""
    src = Path(src)
    notifier.started(src.name)

    on_event = notifier_hook(notifier)

    opts = dataclasses.replace(opts, on_event=on_event)
    try:
        result = convert_fn(src, opts)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        log.error("job failed for %s: %s", src.name, e)
        try:
            write_failure_note(src, inbox, str(e), tb)
        except Exception:  # noqa: BLE001
            log.exception("could not move failed source %s", src)
        notifier.failed(src.name, str(e))
        return False
    try:
        move_source(src, inbox / "done")
    except Exception:  # noqa: BLE001
        log.exception("could not move finished source %s", src)
    notifier.finished(src.name, result)
    return True
