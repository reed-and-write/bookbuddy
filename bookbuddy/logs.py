import logging
import logging.handlers
import sys

import re
import threading

from . import config

# API keys must never reach a log line: known key shapes are masked, and any key the app has handled is masked exactly.
_KEY_SHAPES = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-(?:proj-)?[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{12,})")
_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Remember a key so redact() masks it wherever it shows up (>= 8 chars only, to avoid masking ordinary text)."""
    if value and len(value) >= 8:
        with _lock:
            _secrets.add(value)


def redact(text: str) -> str:
    with _lock:
        known = sorted(_secrets, key=len, reverse=True)
    for s in known:
        text = text.replace(s, "[redacted]")
    return _KEY_SHAPES.sub("[redacted]", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(msg)
        if clean != msg or record.exc_info:
            record.msg, record.args = clean, None
            if record.exc_info:                       # render the traceback now, masked, so handlers don't re-add it
                import traceback
                record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
        return True


def setup_logging(verbose: bool = False, console: bool = True) -> None:
    root = logging.getLogger("bookbuddy")
    if root.handlers:
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        log_dir = config.get_settings().log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "bookbuddy.log", maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        fh.addFilter(RedactFilter())
        root.addHandler(fh)
    except OSError:
        pass
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.addFilter(RedactFilter())
        root.addHandler(sh)
    # chatty third parties
    for n in ("httpx", "urllib3", "huggingface_hub", "filelock"):
        logging.getLogger(n).setLevel(logging.WARNING)
