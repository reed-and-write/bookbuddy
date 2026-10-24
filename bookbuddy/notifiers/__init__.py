"""Notifier plugins. A notifier is a class `Notifier(settings, outbox=Path, dry_run=bool)` with

    send(kind, title, message, status)

`kind` is started | plan | progress | ready | failed; `title`/`message` are for humans, `status` is the one-line
log form. `settings` is that notifier's `[notifiers.<name>]` table from config.toml. Built-ins: `desktop`
(notify-send) and `status-file` (STATUS.txt). Enable them, in order, with `notifiers.enabled` in config.toml.
Third-party notifiers register in the `bookbuddy.notifiers` entry-point group. An exception raised by `send` is
logged and ignored: notifications never fail a job.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..registry import notifiers as registry


class BaseNotifier:
    name = ""

    def __init__(self, settings: Mapping[str, Any] | None = None, *, outbox: Path | None = None, dry_run: bool = False):
        self.settings = dict(settings or {})
        self.outbox = Path(outbox) if outbox else None
        self.dry_run = dry_run

    def send(self, kind: str, title: str, message: str, status: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


from .desktop import DesktopNotifier  # noqa: E402
from .statusfile import StatusFileNotifier  # noqa: E402

registry.register("desktop", DesktopNotifier, builtin=True)
registry.register("status-file", StatusFileNotifier, builtin=True)
