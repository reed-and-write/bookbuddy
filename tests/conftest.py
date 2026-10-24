"""Test isolation: never read the developer's real config.toml/plugins, never touch real data dirs."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_ISO = Path(tempfile.mkdtemp(prefix="bookbuddy-test-"))
os.environ["BOOKBUDDY_CONFIG_DIR"] = str(_ISO / "config")
os.environ["BOOKBUDDY_DATA_DIR"] = str(_ISO / "data")
os.environ["BOOKBUDDY_CACHE_DIR"] = str(_ISO / "cache")
os.environ.pop("BOOKBUDDY_CONFIG", None)
for _k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BOOKBUDDY_TOKEN", "BOOKBUDDY_HOST", "BOOKBUDDY_PORT"):
    os.environ.pop(_k, None)         # tests never see (or use) a real key
# tests exercise the LLM path on tiny clean samples: send every chunk unless a test sets min_score itself
os.environ["BOOKBUDDY_LLM_MIN_SCORE"] = "0"


@pytest.fixture(autouse=True)
def _fresh_settings():
    from bookbuddy import config
    from bookbuddy import registry
    config.reload_settings()
    registry.reset_all()
    yield
    config.set_settings(None)
    registry.reset_all()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A private config/data/cache dir for one test (the library DB, uploads and config.toml live in it)."""
    from bookbuddy import config
    for name, sub in (("BOOKBUDDY_CONFIG_DIR", "config"), ("BOOKBUDDY_DATA_DIR", "data"), ("BOOKBUDDY_CACHE_DIR", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.setenv("BOOKBUDDY_ENGINE", "fake")
    config.reload_settings()
    return tmp_path
