"""Job notifications, delivered through notifier plugins (desktop, status file, third-party). A failure here never fails a job."""
from __future__ import annotations

import logging
from pathlib import Path

from . import config
from .pipeline import fmt_duration
from .registry import notifiers as registry
from . import notifiers as _builtin_notifiers  # noqa: F401  (registers desktop + status-file)

log = logging.getLogger("bookbuddy.notify")


def _fmt_eta(sec: float) -> str:
    mins = max(1, round(sec / 60))
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m"


class Notifier:
    """Formats job events and fans them out to the enabled notifier plugins (errors are logged and swallowed)."""

    def __init__(self, outbox: Path | None = None, dry_run: bool = False, enabled: list[str] | None = None,
                 settings: dict[str, dict] | None = None):
        st = config.get_settings()
        self.outbox = Path(outbox) if outbox else st.outbox
        self.dry_run = dry_run
        names = st.notifiers if enabled is None else enabled
        cfg = st.notifier_settings if settings is None else settings
        self.plugins = []
        for name in names:
            try:
                cls = registry.get(name)
                self.plugins.append((name, cls(cfg.get(name, {}), outbox=self.outbox, dry_run=dry_run)))
            except Exception as e:  # noqa: BLE001 - a missing/broken plugin must not stop the service
                log.warning("notifier %r is enabled but unavailable: %s", name, e)

    def _all(self, kind: str, title: str, msg: str, status: str) -> None:
        for name, plugin in self.plugins:
            try:
                plugin.send(kind, title, msg, status)
            except Exception as e:  # noqa: BLE001 - must never propagate
                log.warning("notification via %s failed: %s", name, e)

    # --- events
    def started(self, filename: str) -> None:
        self._all("started", "Audiobook started", filename, f"START | {filename}")

    def planned(self, title: str, author: str, chapters: int, est_seconds: float, resumed: int = 0, chunks: int = 0) -> None:
        msg = f"{title} by {author}: {chapters} chapters, about {_fmt_eta(est_seconds)} to go"
        if resumed and chunks:
            msg += f" (resuming, {100 * resumed // chunks}% already done)"
        self._all("plan", "Audiobook plan", msg, f"PLAN | {title} | ~{_fmt_eta(est_seconds)}")

    def llm_plan(self, to_send: int, chunks: int, est_seconds: float, provider: str) -> None:
        """PDF only: the LLM cleanup runs before voicing, so its time is announced up front (voicing follows)."""
        if not to_send:
            return
        msg = f"PDF cleanup first: {to_send} of {chunks} sections via {provider}, about {_fmt_eta(est_seconds)}, then voicing"
        self._all("plan", "Audiobook plan", msg, f"PLAN | PDF cleanup {to_send}/{chunks} chunks | ~{_fmt_eta(est_seconds)} before voicing")

    def ocr_plan(self, pages: int, total_pages: int, est_seconds: float, backend: str, cached: bool = False) -> None:
        """Scanned input: reading the page images comes first, so its time is announced up front (cleanup and voicing follow)."""
        if not pages or cached:
            return
        what = f"{pages} of {total_pages} pages" if pages != total_pages else f"{pages} pages"
        self._all("plan", "Audiobook plan", f"Reading scanned pages first (OCR via {backend}): {what}, about {_fmt_eta(est_seconds)}, then voicing",
                  f"PLAN | OCR {pages}/{total_pages} pages | ~{_fmt_eta(est_seconds)} before voicing")

    def ocr_progress(self, done: int, pages: int, percent: int, remaining_seconds: float | None) -> None:
        if percent >= 100:
            return
        eta = f", about {_fmt_eta(remaining_seconds)} left" if remaining_seconds else ""
        self._all("progress", "Audiobook progress", f"OCR: {done} of {pages} pages read{eta}", f"PROGRESS | OCR | {done}/{pages} pages{eta}")

    def progress(self, title: str, percent: int, remaining_seconds: float | None) -> None:
        eta = f", about {_fmt_eta(remaining_seconds)} left" if remaining_seconds else ""
        self._all("progress", "Audiobook progress", f"{title}: {percent}% voiced{eta}", f"PROGRESS | {title} | {percent}%{eta}")

    def finished(self, filename: str, result) -> None:
        size = f"{result.size / 1e6:.1f} MB"
        llm = getattr(result, "llm", "none")
        msg = f"{result.title} by {result.author} is ready ({fmt_duration(result.duration)}, {size})"
        if Path(filename).suffix.lower() == ".pdf":
            msg += f" [PDF cleanup: {llm}]"
        self._all("ready", "Audiobook ready", msg,
                  f"OK | {result.title} | {result.author} | {fmt_duration(result.duration)} | {size} | LLM {llm} | {result.path.name}")

    def failed(self, filename: str, reason: str) -> None:
        reason = reason.strip().splitlines()[-1][:200] if reason.strip() else "unknown error"
        self._all("failed", "Audiobook FAILED", f"{filename}: {reason}", f"FAIL | {filename} | {reason}")
