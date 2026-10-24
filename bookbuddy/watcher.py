"""Inbox watcher: waits for Dropbox to finish syncing a file, then converts it. One book at a time."""
from __future__ import annotations

import argparse
import logging
import signal
import time
from pathlib import Path

from . import config
from .jobs import process_file
from .worker import server_running
from .logs import setup_logging
from .notify import Notifier
from .extractors import is_supported_name, supported_extensions
from .pipeline import Options

log = logging.getLogger("bookbuddy.watcher")

TEMP_MARKERS = (".tmp", ".part", ".partial", ".crdownload", ".dropbox", ".download", ".swp")


def is_candidate(p: Path) -> bool:
    n = p.name
    return (p.is_file() and is_supported_name(n) and not n.startswith((".", "~", "._"))
            and not n.lower().endswith(TEMP_MARKERS))


def has_temp_sibling(p: Path) -> bool:
    """True if a sync/partial-download temp file for this book sits beside it."""
    stem, name = p.stem.lower(), p.name.lower()
    try:
        siblings = list(p.parent.iterdir())
    except OSError:
        return True
    for s in siblings:
        n = s.name.lower()
        if s == p or n == name:
            continue
        looks_temp = n.endswith(TEMP_MARKERS) or n.startswith((".", "~$", "~")) and (".tmp" in n or ".dropbox" in n or n.startswith("~$"))
        if looks_temp and (name in n or stem in n):
            return True
    return False


class SettleTracker:
    """A file is 'settled' once size+mtime stay unchanged for `settle` seconds and no temp sibling exists."""

    def __init__(self, settle: float = 10.0, clock=time.monotonic):
        self.settle = settle
        self.clock = clock
        self._seen: dict[Path, tuple[int, float, float]] = {}   # path -> (size, mtime, stable_since)

    def check(self, p: Path) -> bool:
        try:
            st = p.stat()
        except OSError:
            self._seen.pop(p, None)
            return False
        now = self.clock()
        sig = (st.st_size, st.st_mtime)
        prev = self._seen.get(p)
        if prev is None or (prev[0], prev[1]) != sig or st.st_size == 0:
            self._seen[p] = (sig[0], sig[1], now)
            return False
        if has_temp_sibling(p):
            self._seen[p] = (sig[0], sig[1], now)
            return False
        return now - prev[2] >= self.settle

    def forget(self, p: Path) -> None:
        self._seen.pop(p, None)


def scan(inbox: Path) -> list[Path]:
    files = [p for p in inbox.iterdir() if is_candidate(p)]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def run_once(inbox: Path, tracker: SettleTracker, opts: Options, notifier: Notifier, skip: set[Path]) -> int:
    """Process at most one settled book. Returns number processed."""
    for p in scan(inbox):
        if p in skip:
            continue
        if tracker.check(p):
            tracker.forget(p)
            ok = process_file(p, inbox, opts, notifier)
            if not ok and p.exists():
                skip.add(p)   # could not be moved away; do not loop forever
            return 1
    return 0


def enqueue_ready(inbox: Path, tracker: SettleTracker, args, lib, skip: set[Path]) -> int:
    """A server owns the GPU: hand every settled book to its queue (origin 'watch') instead of converting here.
    The source stays in the inbox until the server finishes it (then it moves to done/ or failed/ as always)."""
    from . import intake

    n = 0
    for p in scan(inbox):
        if p in skip or lib.active_job_for_source(str(p)) or not tracker.check(p):
            continue
        tracker.forget(p)
        try:
            staged, sha, _ = intake.stage_path(p)
            extra = {"out_dir": str(args.out), "inbox": str(inbox), "source_path": str(p)}
            book_id, job_id = intake.enqueue_file(lib, staged, sha, origin="watch", voice=args.voice, speed=args.speed,
                                                  fmt=args.fmt, llm="none" if args.local_only else args.llm, extra=extra,
                                                  original_name=p.name)
            log.info("queued %s on the server as job %d (book %d)", p.name, job_id, book_id)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.error("could not queue %s: %s", p.name, e)
            skip.add(p)
    return n


def add_watch_arguments(ap: argparse.ArgumentParser) -> None:
    st = config.get_settings()
    ap.add_argument("--inbox", type=Path, default=st.watch_inbox, help=f"folder to watch (default: {st.watch_inbox})")
    ap.add_argument("--out", type=Path, default=st.outbox, help=f"where finished audiobooks go (default: {st.outbox})")
    ap.add_argument("--voice", default=st.voice, help="Kokoro voice (default: %(default)s)")
    ap.add_argument("--speed", type=float, default=st.speed, help="speaking speed (default: %(default)s)")
    ap.add_argument("--format", dest="fmt", choices=["m4b", "mp3"], default=st.format, help="output format (default: %(default)s)")
    ap.add_argument("--poll", type=float, default=st.poll, help="seconds between inbox scans")
    ap.add_argument("--settle", type=float, default=st.settle, help="seconds a file must be unchanged before processing")
    ap.add_argument("--dry-run", action="store_true", help="print notifications instead of sending them")
    ap.add_argument("--once", action="store_true", help="process whatever is ready, then exit")
    ap.add_argument("--llm", default=None, metavar="PROVIDER", help="PDF cleanup provider: auto | none | an installed name")
    ap.add_argument("--local-only", action="store_true", help="same as --llm none")
    ap.add_argument("--keep-work", action="store_true")
    ap.add_argument("--device", choices=["cuda", "cpu"], default=st.device)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.set_defaults(func=run_watch)


def run_watch(args) -> int:
    setup_logging(args.verbose)
    args.inbox.mkdir(parents=True, exist_ok=True)
    notifier = Notifier(args.out, dry_run=args.dry_run)
    opts = Options(voice=args.voice, speed=args.speed, fmt=args.fmt, out_dir=args.out, keep_work=args.keep_work,
                   device=args.device, llm="none" if args.local_only else args.llm)
    tracker = SettleTracker(args.settle)
    skip: set[Path] = set()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    log.info("watching %s (poll %.0fs, settle %.0fs, dry-run=%s, formats %s)", args.inbox, args.poll, args.settle,
             args.dry_run, " ".join(sorted(supported_extensions())))

    lib = None
    while not stop["flag"]:
        try:
            if server_running():
                if lib is None:
                    from .library import Library
                    lib = Library(config.get_settings().db_path)
                    log.info("a Book Buddy server is running: books go to its queue")
                n = enqueue_ready(args.inbox, tracker, args, lib, skip)
                pending = any(p not in skip and not lib.active_job_for_source(str(p)) for p in scan(args.inbox))
            else:
                n = run_once(args.inbox, tracker, opts, notifier, skip)
                pending = scan_pending(args.inbox, skip)
        except Exception:  # noqa: BLE001 - keep the service alive
            log.exception("watcher loop error")
            n, pending = 0, True
        if args.once and n == 0 and not pending:
            break
        if n == 0:
            time.sleep(args.poll)
    log.info("watcher stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bookbuddy watch")
    add_watch_arguments(ap)
    return run_watch(ap.parse_args(argv))


def scan_pending(inbox: Path, skip: set[Path]) -> bool:
    return any(p not in skip for p in scan(inbox))


if __name__ == "__main__":
    raise SystemExit(main())
