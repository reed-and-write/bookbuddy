"""Desktop notifications through `notify-send` (Linux; a no-op error elsewhere, which is logged and ignored)."""
from __future__ import annotations

import shlex
import subprocess

from . import BaseNotifier


class DesktopNotifier(BaseNotifier):
    name = "desktop"

    def send(self, kind: str, title: str, message: str, status: str) -> None:
        cmd = ["notify-send", "-a", "bookbuddy", title, message]
        if self.dry_run:
            print("[dry-run] " + " ".join(shlex.quote(c) for c in cmd), flush=True)
            return
        subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
