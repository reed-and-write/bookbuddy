import stat
import tomllib

import pytest

from bookbuddy.config_edit import update_config


def test_creates_file_mode_0600_with_tables(tmp_path):
    p = tmp_path / "sub" / "config.toml"
    update_config(p, {("llm.anthropic", "api_key"): "sk-x", ("web", "donate_url"): "https://e.org", ("", "voice"): "af_heart"})
    d = tomllib.loads(p.read_text())
    assert d == {"voice": "af_heart", "web": {"donate_url": "https://e.org"}, "llm": {"anthropic": {"api_key": "sk-x"}}}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_preserves_comments_and_other_tables_and_replaces_in_place(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# top\nvoice = "a"  # keep me\nspeed = 1.0\n\n[llm]\nprovider = "auto"\norder = [\n  "first",\n  "second",\n]\nworkers = 5\n\n[llm.first]\nmodel = "haiku"\n')
    update_config(p, {("", "voice"): "b", ("llm", "order"): ["x", "y"], ("llm.first", "model"): "m", ("llm.first", "extra"): True})
    t = p.read_text()
    assert "# top" in t and 'voice = "b"' in t and "keep me" not in t        # a replaced line loses only its own trailing comment
    d = tomllib.loads(t)
    assert d["llm"]["order"] == ["x", "y"] and d["llm"]["workers"] == 5 and d["llm"]["provider"] == "auto"
    assert d["llm"]["first"] == {"model": "m", "extra": True} and d["speed"] == 1.0


def test_remove_key_and_missing_is_noop(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[llm.openai]\napi_key = "sk-1"\nmodel = "m"\n')
    update_config(p, {("llm.openai", "api_key"): None, ("nope", "x"): None})
    assert tomllib.loads(p.read_text()) == {"llm": {"openai": {"model": "m"}}}


def test_strings_are_escaped_and_roundtrip(tmp_path):
    p = tmp_path / "c.toml"
    nasty = 'quote" back\\slash and # hash\nnewline'
    update_config(p, {("web", "donate_url"): nasty})
    assert tomllib.loads(p.read_text())["web"]["donate_url"] == nasty


def test_top_level_key_goes_before_first_table(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[paths]\nwork_dir = '/w'\n")
    update_config(p, {("", "speed"): 1.25})
    assert tomllib.loads(p.read_text()) == {"speed": 1.25, "paths": {"work_dir": "/w"}}


def test_never_writes_an_unreadable_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("this is = = not toml\n")
    with pytest.raises(Exception):
        update_config(p, {("", "speed"): 1.0})
    assert p.read_text() == "this is = = not toml\n"
