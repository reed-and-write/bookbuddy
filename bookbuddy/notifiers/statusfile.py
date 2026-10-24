"""Append one line per event to a status file (default: STATUS.txt in the output folder)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import BaseNotifier


class StatusFileNotifier(BaseNotifier):
    name = "status-file"

    def send(self, kind: str, title: str, message: str, status: str) -> None:
        path = self.settings.get("path")
        path = Path(path).expanduser() if path else (self.outbox or Path.cwd()) / "STATUS.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {status}\n")
