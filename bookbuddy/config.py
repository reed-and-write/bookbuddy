"""Constants plus user settings.

Settings come from (lowest to highest priority): built-in defaults, `config.toml`, `BOOKBUDDY_*` environment
variables, command-line flags. Locations follow platformdirs:

    config   ~/.config/bookbuddy/config.toml (and ./plugins/ for drop-in plugin files)
    data     ~/.local/share/bookbuddy/{work,logs}
    cache    ~/.cache/bookbuddy/{llm}
    books    ~/BookBuddy  (finished audiobooks; the watch inbox is ~/BookBuddy/inbox)

`BOOKBUDDY_CONFIG` (a file), `BOOKBUDDY_CONFIG_DIR`, `BOOKBUDDY_DATA_DIR` and `BOOKBUDDY_CACHE_DIR` relocate them.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import platformdirs

APP_NAME = "bookbuddy"

# --- audio / chunking constants (not user settings)
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0
SAMPLE_RATE = 24000          # Kokoro native rate
AAC_BITRATE = "64k"
MAX_CHUNK_CHARS = 400

# Pauses (seconds) appended after a chunk
PAUSE_SENTENCE = 0.30
PAUSE_PARAGRAPH = 0.75
PAUSE_AFTER_TITLE = 1.20
PAUSE_CHAPTER_END = 1.50

PIPELINE_VERSION = "1"       # bump to invalidate old checkpoints
OCR_BACKENDS = ("auto", "ocrmypdf", "tesseract")
DEFAULT_DONATE_URL = "https://github.com/sponsors/reed-and-write"
DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_MB = 1024
PDF_PART_WORDS = 3000        # fallback section size when a PDF has no detectable chapters


def config_dir() -> Path:
    return Path(os.environ.get("BOOKBUDDY_CONFIG_DIR") or platformdirs.user_config_dir(APP_NAME))


def data_dir() -> Path:
    return Path(os.environ.get("BOOKBUDDY_DATA_DIR") or platformdirs.user_data_dir(APP_NAME))


def cache_dir() -> Path:
    return Path(os.environ.get("BOOKBUDDY_CACHE_DIR") or platformdirs.user_cache_dir(APP_NAME))


def config_path() -> Path:
    return Path(os.environ.get("BOOKBUDDY_CONFIG") or config_dir() / "config.toml")


def plugins_dir() -> Path:
    return config_dir() / "plugins"


def _path(v: Any) -> Path:
    return Path(os.path.expanduser(str(v)))


@dataclass
class Settings:
    # conversion
    voice: str = DEFAULT_VOICE
    speed: float = DEFAULT_SPEED
    format: str = "m4b"                       # m4b | mp3
    device: str | None = None                 # None = auto (CUDA, then CPU)
    engine: str = "kokoro"                    # name in the bookbuddy.engines registry
    output_dir: Path = field(default_factory=lambda: Path.home() / "BookBuddy")
    # watch folder
    watch_inbox: Path = field(default_factory=lambda: Path.home() / "BookBuddy" / "inbox")
    watch_outbox: Path | None = None          # None = output_dir
    poll: float = 5.0
    settle: float = 10.0
    # LLM cleanup (PDF text only; off unless a provider plugin is installed/configured)
    llm_enabled: bool = False                 # opt-in: no LLM pass (and no env key use) until the user turns it on
    llm_provider: str = "auto"                # auto | none | <provider name>
    llm_order: list[str] = field(default_factory=lambda: ["anthropic", "openai"])   # auto = these, in order (providers without a key are skipped)
    llm_providers: dict[str, dict] = field(default_factory=dict)   # per-provider keyword settings
    llm_timeout: float = 240.0
    llm_chunk_chars: int = 4500
    llm_workers: int = 5
    llm_min_score: float = 0.5                # chunks scoring below this skip the LLM (0 = send all)
    llm_progress_every: int = 10
    # text extraction (Markdown / HTML / DOCX / ODT)
    code_blocks: str = "skip"                 # skip | announce ("Code block omitted.")
    tables: str = "skip"                      # skip | read (rows read as comma-separated lines)
    # OCR (scanned PDFs, image folders, CBZ/CBR, DJVU)
    ocr_lang: str = "eng"                     # Tesseract language code(s), e.g. "eng" or "eng+deu"
    ocr_backend: str = "auto"                 # auto | ocrmypdf | tesseract
    ocr_workers: int = 0                      # 0 = auto (half the CPU cores, at most 8)
    ocr_progress_every: int = 10              # emit ocr_progress every N pages
    # web server (`bookbuddy serve`)
    web_host: str = "127.0.0.1"
    web_port: int = DEFAULT_PORT
    web_token: str | None = None              # required for any non-loopback host
    web_max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB
    donate_url: str = DEFAULT_DONATE_URL
    # notifications
    notifiers: list[str] = field(default_factory=lambda: ["desktop", "status-file"])
    notifier_settings: dict[str, dict] = field(default_factory=dict)
    # locations
    work_dir: Path = field(default_factory=lambda: data_dir() / "work")
    log_dir: Path = field(default_factory=lambda: data_dir() / "logs")
    cache_dir: Path = field(default_factory=cache_dir)
    hf_home: Path | None = None               # None = the normal Hugging Face cache
    source: Path | None = None                # config file that was read (None = defaults only)

    @property
    def outbox(self) -> Path:
        return self.watch_outbox or self.output_dir

    @property
    def db_path(self) -> Path:
        return data_dir() / "library.db"

    @property
    def library_dir(self) -> Path:
        return data_dir() / "library"

    @property
    def trash_dir(self) -> Path:
        return data_dir() / "trash"


_TOP_KEYS = {"voice", "speed", "format", "device", "engine", "output_dir"}


def parse_settings(data: Mapping[str, Any], env: Mapping[str, str] | None = None, source: Path | None = None) -> Settings:
    """Build Settings from parsed TOML plus an environment mapping (pure: easy to test)."""
    env = os.environ if env is None else env
    s = Settings(source=source)
    top = {k: v for k, v in data.items() if k in _TOP_KEYS}
    paths, watch, llm, notif, extract, ocr, web = (data.get(k, {}) for k in ("paths", "watch", "llm", "notifiers", "extract", "ocr", "web"))
    for name in ("paths", "watch", "llm", "notifiers", "extract", "ocr", "web"):
        if not isinstance(data.get(name, {}), dict):
            raise ValueError(f"config: [{name}] must be a table")

    if "voice" in top: s.voice = str(top["voice"])
    if "speed" in top: s.speed = float(top["speed"])
    if "format" in top:
        if top["format"] not in ("m4b", "mp3"):
            raise ValueError("config: format must be 'm4b' or 'mp3'")
        s.format = top["format"]
    if top.get("device"): s.device = str(top["device"])
    if "engine" in top: s.engine = str(top["engine"])
    if "output_dir" in top:
        s.output_dir = _path(top["output_dir"])
        s.watch_inbox = s.output_dir / "inbox"

    if "work_dir" in paths: s.work_dir = _path(paths["work_dir"])
    if "log_dir" in paths: s.log_dir = _path(paths["log_dir"])
    if "cache_dir" in paths: s.cache_dir = _path(paths["cache_dir"])
    if paths.get("hf_home"): s.hf_home = _path(paths["hf_home"])

    if "inbox" in watch: s.watch_inbox = _path(watch["inbox"])
    if "outbox" in watch: s.watch_outbox = _path(watch["outbox"])
    if "poll" in watch: s.poll = float(watch["poll"])
    if "settle" in watch: s.settle = float(watch["settle"])

    if "enabled" in llm: s.llm_enabled = bool(llm["enabled"])
    if "provider" in llm: s.llm_provider = str(llm["provider"])
    if "order" in llm: s.llm_order = [str(x) for x in llm["order"]]
    for k, v in llm.items():           # [llm.<provider>] tables
        if isinstance(v, dict):
            s.llm_providers[k] = dict(v)
    for key, attr, cast in (("timeout", "llm_timeout", float), ("chunk_chars", "llm_chunk_chars", int),
                            ("workers", "llm_workers", int), ("min_score", "llm_min_score", float),
                            ("progress_every", "llm_progress_every", int)):
        if key in llm:
            setattr(s, attr, cast(llm[key]))

    for key, attr, allowed in (("code_blocks", "code_blocks", ("skip", "announce")), ("tables", "tables", ("skip", "read"))):
        if key in extract:
            if extract[key] not in allowed:
                raise ValueError(f"config: [extract] {key} must be one of {', '.join(allowed)}")
            setattr(s, attr, extract[key])

    if "lang" in ocr: s.ocr_lang = str(ocr["lang"]).strip() or "eng"
    if "backend" in ocr:
        if ocr["backend"] not in OCR_BACKENDS:
            raise ValueError(f"config: [ocr] backend must be one of {', '.join(OCR_BACKENDS)}")
        s.ocr_backend = ocr["backend"]
    if "workers" in ocr: s.ocr_workers = max(0, int(ocr["workers"]))
    if "progress_every" in ocr: s.ocr_progress_every = max(1, int(ocr["progress_every"]))

    if "host" in web: s.web_host = str(web["host"])
    if "port" in web: s.web_port = int(web["port"])
    if web.get("token"): s.web_token = str(web["token"])
    if "max_upload_mb" in web: s.web_max_upload_mb = max(1, int(web["max_upload_mb"]))
    if web.get("donate_url"): s.donate_url = str(web["donate_url"])

    if "enabled" in notif: s.notifiers = [str(x) for x in notif["enabled"]]
    for k, v in notif.items():         # [notifiers.<name>] tables
        if isinstance(v, dict):
            s.notifier_settings[k] = dict(v)

    # environment overrides (also used by the test-suite)
    e = env.get
    if e("BOOKBUDDY_ENGINE"): s.engine = e("BOOKBUDDY_ENGINE")
    if e("BOOKBUDDY_VOICE"): s.voice = e("BOOKBUDDY_VOICE")
    if e("BOOKBUDDY_LLM"): s.llm_provider = e("BOOKBUDDY_LLM")
    if e("BOOKBUDDY_LLM_TIMEOUT"): s.llm_timeout = float(e("BOOKBUDDY_LLM_TIMEOUT"))
    if e("BOOKBUDDY_LLM_WORKERS"): s.llm_workers = int(e("BOOKBUDDY_LLM_WORKERS"))
    if e("BOOKBUDDY_LLM_MIN_SCORE"): s.llm_min_score = float(e("BOOKBUDDY_LLM_MIN_SCORE"))
    if e("BOOKBUDDY_LLM_PROGRESS_EVERY"): s.llm_progress_every = int(e("BOOKBUDDY_LLM_PROGRESS_EVERY"))
    if e("BOOKBUDDY_CODE_BLOCKS") in ("skip", "announce"): s.code_blocks = e("BOOKBUDDY_CODE_BLOCKS")
    if e("BOOKBUDDY_TABLES") in ("skip", "read"): s.tables = e("BOOKBUDDY_TABLES")
    if e("BOOKBUDDY_OCR_LANG"): s.ocr_lang = e("BOOKBUDDY_OCR_LANG")
    if e("BOOKBUDDY_OCR_BACKEND") in OCR_BACKENDS: s.ocr_backend = e("BOOKBUDDY_OCR_BACKEND")
    if e("BOOKBUDDY_OCR_WORKERS"): s.ocr_workers = max(0, int(e("BOOKBUDDY_OCR_WORKERS")))
    if e("BOOKBUDDY_TOKEN"): s.web_token = e("BOOKBUDDY_TOKEN")
    if e("BOOKBUDDY_HOST"): s.web_host = e("BOOKBUDDY_HOST")
    if e("BOOKBUDDY_PORT"): s.web_port = int(e("BOOKBUDDY_PORT"))
    if e("BOOKBUDDY_WORK"): s.work_dir = _path(e("BOOKBUDDY_WORK"))
    if e("BOOKBUDDY_LOGS"): s.log_dir = _path(e("BOOKBUDDY_LOGS"))
    if e("BOOKBUDDY_CACHE"): s.cache_dir = _path(e("BOOKBUDDY_CACHE"))
    if e("BOOKBUDDY_INBOX"): s.watch_inbox = _path(e("BOOKBUDDY_INBOX"))
    if e("BOOKBUDDY_OUTBOX"): s.watch_outbox = _path(e("BOOKBUDDY_OUTBOX"))
    if e("BOOKBUDDY_OUTPUT_DIR"): s.output_dir = _path(e("BOOKBUDDY_OUTPUT_DIR"))
    return s


def load_settings(path: Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    path = Path(path) if path else config_path()
    data: dict = {}
    src = None
    if path.is_file():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path}: {e}") from e
        src = path
    return parse_settings(data, env, src)


_cached: Settings | None = None


def get_settings() -> Settings:
    """The process-wide settings (loaded once; call reload_settings() after changing the environment)."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached


def reload_settings() -> Settings:
    global _cached
    _cached = None
    return get_settings()


def set_settings(s: Settings | None) -> None:
    global _cached
    _cached = s


def setup_env() -> None:
    """Point the Hugging Face cache somewhere specific when the user asked for it."""
    hf = get_settings().hf_home
    if hf is not None:
        os.environ.setdefault("HF_HOME", str(hf))
