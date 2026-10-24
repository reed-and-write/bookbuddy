import os
import subprocess
import sys
from pathlib import Path

import pytest

from bookbuddy import config
from bookbuddy.config import load_settings, parse_settings

ROOT = Path(__file__).resolve().parent.parent


def test_defaults_follow_platformdirs(monkeypatch, tmp_path):
    for k in ("BOOKBUDDY_CONFIG_DIR", "BOOKBUDDY_DATA_DIR", "BOOKBUDDY_CACHE_DIR", "BOOKBUDDY_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert config.config_path() == tmp_path / "cfg" / "bookbuddy" / "config.toml"
    s = load_settings()
    assert s.work_dir == tmp_path / "data" / "bookbuddy" / "work"
    assert s.cache_dir == tmp_path / "cache" / "bookbuddy"
    assert s.output_dir == Path.home() / "BookBuddy" and s.watch_inbox == Path.home() / "BookBuddy" / "inbox"
    assert s.outbox == s.output_dir and s.notifiers == ["desktop", "status-file"] and s.llm_order == ["anthropic", "openai"] and s.source is None


def test_config_file_values(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('''
voice = "bf_emma"
speed = 1.25
format = "mp3"
output_dir = "~/Books/out"
[paths]
hf_home = "/models/hf"
work_dir = "/w"
[watch]
inbox = "/in"
outbox = "/out"
poll = 2
settle = 3
[llm]
provider = "auto"
order = ["a", "b"]
workers = 2
min_score = 0.8
[llm.a]
model = "m1"
[notifiers]
enabled = ["status-file", "x"]
[notifiers.x]
target = "t"
''')
    s = load_settings(f, env={})
    assert (s.voice, s.speed, s.format) == ("bf_emma", 1.25, "mp3")
    assert s.output_dir == Path.home() / "Books/out" and s.hf_home == Path("/models/hf") and s.work_dir == Path("/w")
    assert (s.watch_inbox, s.outbox, s.poll, s.settle) == (Path("/in"), Path("/out"), 2.0, 3.0)
    assert s.llm_order == ["a", "b"] and s.llm_workers == 2 and s.llm_min_score == 0.8 and s.llm_providers == {"a": {"model": "m1"}}
    assert s.notifiers == ["status-file", "x"] and s.notifier_settings == {"x": {"target": "t"}} and s.source == f


def test_env_overrides_file_and_output_dir_moves_inbox(tmp_path):
    s = parse_settings({"output_dir": "/books", "voice": "a"}, env={"BOOKBUDDY_VOICE": "b", "BOOKBUDDY_ENGINE": "fake",
                                                                    "BOOKBUDDY_LLM_MIN_SCORE": "0", "BOOKBUDDY_WORK": "/w2"})
    assert s.voice == "b" and s.engine == "fake" and s.llm_min_score == 0 and s.work_dir == Path("/w2")
    assert s.watch_inbox == Path("/books/inbox")


def test_bad_config_is_a_clear_error(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("voice = [unclosed")
    with pytest.raises(ValueError, match="c.toml"):
        load_settings(f)
    f.write_text('format = "wav"')
    with pytest.raises(ValueError, match="format"):
        load_settings(f)


def test_get_settings_reads_the_configured_file(tmp_path, monkeypatch):
    f = tmp_path / "mine.toml"
    f.write_text('voice = "zz"')
    monkeypatch.setenv("BOOKBUDDY_CONFIG", str(f))
    assert config.reload_settings().voice == "zz"


def test_cli_uses_config_defaults(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('voice = "bf_emma"\n[watch]\ninbox = "/some/inbox"\n')
    env = dict(os.environ, BOOKBUDDY_CONFIG=str(cfg), PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "bookbuddy.cli", "listen", "--help"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "bf_emma" in r.stdout
    r = subprocess.run([sys.executable, "-m", "bookbuddy.cli", "watch", "--help"], env=env, capture_output=True, text=True)
    assert "/some/inbox" in r.stdout
    env["BOOKBUDDY_DATA_DIR"] = str(tmp_path / "data")
    r = subprocess.run([sys.executable, "-m", "bookbuddy.cli", "queue"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "queue is empty" in r.stdout
    r = subprocess.run([sys.executable, "-m", "bookbuddy.cli", "serve", "--help"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "--token" in r.stdout
