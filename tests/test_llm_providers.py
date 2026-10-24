"""Bring-your-own-key providers (mocked SDK clients), order/fallback, no-key = no LLM, key redaction in logs."""
import io
import logging
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

from bookbuddy import config, llm_providers, logs
from bookbuddy.llm_clean import Cleaner, ProviderError, make_providers, resolve_order
from bookbuddy.llm_providers import AnthropicProvider, OpenAIProvider
from bookbuddy.registry import llm_providers as registry

KEY_A = "sk-" + "ant-api03-" + "A" * 24          # built at runtime: a fake key, not a literal secret
KEY_O = "sk-" + "proj-" + "O" * 28
TEXT = ("It was a dark night and the rain fell on the old road. Mr. Smith walked home alone.\n\n"
        "He had a long way to go before he reached the gate of his house.")


def resp(status):
    return httpx.Response(status, request=httpx.Request("POST", "https://example.invalid"))


def anthropic_client(reply=None, error=None, seen=None):
    def create(**kw):
        if seen is not None:
            seen.append(kw)
        if error:
            raise error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def openai_client(reply=None, error=None, seen=None):
    def create(**kw):
        if seen is not None:
            seen.append(kw)
        if error:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def with_client(cls, client, **settings):
    p = cls(**settings)
    p._client = client
    return p


def test_default_models_and_licenses():
    assert llm_providers.DEFAULT_ANTHROPIC_MODEL == "claude-haiku-5-5"
    p = AnthropicProvider(api_key=KEY_A)
    assert p.model == "claude-haiku-5-5" and OpenAIProvider(api_key=KEY_O).model == llm_providers.DEFAULT_OPENAI_MODEL
    assert AnthropicProvider(api_key=KEY_A, model="claude-other").model == "claude-other"       # overridable


def test_anthropic_success_sends_the_prompt_and_model(env):
    seen = []
    p = with_client(AnthropicProvider, anthropic_client(TEXT, seen=seen), api_key=KEY_A)
    out = Cleaner([p], cache_dir=None, min_score=0).clean_chunk(TEXT, force=True)
    assert out == TEXT.strip()
    assert seen[0]["model"] == "claude-haiku-5-5" and TEXT in seen[0]["messages"][0]["content"] and seen[0]["max_tokens"] > 1000


def test_openai_success_uses_chat_completions(env):
    seen = []
    p = with_client(OpenAIProvider, openai_client(TEXT, seen=seen), api_key=KEY_O, model="gpt-test")
    assert Cleaner([p], min_score=0).clean_chunk(TEXT, force=True) == TEXT.strip()
    assert seen[0]["model"] == "gpt-test" and "max_completion_tokens" in seen[0]


def test_rejected_output_keeps_the_original_text(tmp_path):
    summary = "This chapter is about a man walking home in the rain."
    p = with_client(AnthropicProvider, anthropic_client(summary), api_key=KEY_A)
    c = Cleaner([p], cache_dir=tmp_path, min_score=0)
    assert c.clean_chunk(TEXT, force=True) == TEXT           # the existing validator refuses a rewrite
    assert c.stats["rejected"] == 1


def test_auth_failure_is_fatal_and_falls_back_to_the_next_provider(tmp_path):
    bad = with_client(AnthropicProvider, anthropic_client(error=anthropic.AuthenticationError("bad key", response=resp(401), body=None)), api_key=KEY_A)
    good = with_client(OpenAIProvider, openai_client(TEXT), api_key=KEY_O)
    c = Cleaner([bad, good], cache_dir=tmp_path, min_score=0)
    assert c.clean_chunk(TEXT, force=True) == TEXT.strip()
    assert "anthropic" in c.dead and c.used == {"openai": 1}
    c.clean_chunk(TEXT + " extra words here for a new key.", force=True)
    assert c.stats["failed"] == 1                            # the dead provider is not asked again


def test_rate_limit_is_soft_then_fallback_still_works(tmp_path):
    flaky = with_client(AnthropicProvider, anthropic_client(error=anthropic.RateLimitError("slow down", response=resp(429), body=None)), api_key=KEY_A)
    good = with_client(OpenAIProvider, openai_client(TEXT), api_key=KEY_O)
    c = Cleaner([flaky, good], cache_dir=tmp_path, min_score=0)
    assert c.clean_chunk(TEXT, force=True) == TEXT.strip() and "anthropic" not in c.dead


def test_every_provider_failing_returns_the_original(tmp_path, env):
    a = with_client(AnthropicProvider, anthropic_client(error=ConnectionError("down")), api_key=KEY_A)
    o = with_client(OpenAIProvider, openai_client(error=openai.AuthenticationError("no", response=resp(401), body=None)), api_key=KEY_O)
    assert Cleaner([a, o], cache_dir=tmp_path, min_score=0).clean_chunk(TEXT, force=True) == TEXT


def test_provider_without_key_raises_fatal():
    with pytest.raises(ProviderError) as e:
        AnthropicProvider().run("hi")
    assert e.value.fatal


def _write_cfg(text):
    cfg = config.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.toml").write_text(text)
    config.reload_settings()


def test_order_is_anthropic_then_openai_and_needs_keys(env, monkeypatch):
    _write_cfg("[llm]\nenabled = true\n")
    assert resolve_order("auto") == []                                 # no key, no LLM
    monkeypatch.setenv("OPENAI_API_KEY", KEY_O)
    assert resolve_order("auto") == ["openai"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_A)
    assert resolve_order("auto") == ["anthropic", "openai"]
    assert [p.name for p in make_providers(resolve_order("auto"))] == ["anthropic", "openai"]
    assert resolve_order("none") == []
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="no API key"):
        resolve_order("anthropic")


def test_no_key_means_no_sdk_client_is_ever_built(env, monkeypatch):
    def boom(self):
        raise AssertionError("an SDK client must not be created without a key")
    monkeypatch.setattr(AnthropicProvider, "_make_client", boom)
    monkeypatch.setattr(OpenAIProvider, "_make_client", boom)
    c = Cleaner.from_choice("auto")
    assert c.providers == [] and c.clean_chunk(TEXT) == TEXT and c.clean_chapters([[TEXT]]) == [[TEXT]]


def test_key_from_config_beats_env_and_order_can_be_changed(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-1234567890")
    cfg = config.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.toml").write_text(f'[llm]\nenabled = true\norder = ["openai", "anthropic"]\n[llm.anthropic]\napi_key = "{KEY_A}"\nmodel = "claude-x"\n[llm.openai]\napi_key = "{KEY_O}"\n')
    config.reload_settings()
    ps = make_providers(resolve_order("auto"))
    assert [p.name for p in ps] == ["openai", "anthropic"]
    assert ps[1].api_key == KEY_A and ps[1].model == "claude-x"


def test_local_plugin_providers_still_work_and_can_be_ordered(env):
    class Local(llm_providers.Provider):
        name = "claude"
        def run(self, prompt):
            return TEXT
    registry.register("claude", Local)
    cfg = config.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.toml").write_text('[llm]\nenabled = true\norder = ["claude", "anthropic", "openai"]\n')
    config.reload_settings()
    assert resolve_order("auto") == ["claude"]                          # plugin providers need no key; keyed ones are skipped
    assert Cleaner.from_choice("auto", min_score=0).clean_chunk(TEXT, force=True) == TEXT.strip()


# ------------------------------------------------------------------ AI cleanup is explicit opt-in

def test_env_key_alone_does_not_enable_the_llm_pass(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_A)
    monkeypatch.setenv("OPENAI_API_KEY", KEY_O)
    config.reload_settings()
    assert config.get_settings().llm_enabled is False
    assert resolve_order("auto") == []
    assert Cleaner.from_choice("auto").providers == []


def test_no_sdk_client_is_built_for_an_env_key_while_disabled(env, monkeypatch):
    def boom(self):
        raise AssertionError("must not contact a provider while the LLM pass is off")
    monkeypatch.setattr(AnthropicProvider, "_make_client", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_A)
    c = Cleaner.from_choice("auto")
    assert c.providers == [] and c.clean_chunk(TEXT) == TEXT


def test_enabled_in_config_lets_env_keys_work(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_A)
    _write_cfg("[llm]\nenabled = true\n")
    assert resolve_order("auto") == ["anthropic"]


def test_explicit_provider_choice_is_itself_the_opt_in(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_A)
    assert resolve_order("anthropic") == ["anthropic"]                 # e.g. --llm anthropic
    assert resolve_order("auto") == []                                 # ...but only for that call


def test_local_only_forces_off_even_when_enabled(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY_A)
    _write_cfg("[llm]\nenabled = true\n")
    assert resolve_order("none") == []
    from bookbuddy import cli
    seen = {}

    def fake_convert(book, opts):
        seen["llm"] = opts.llm
        raise RuntimeError("stop")

    monkeypatch.setattr("bookbuddy.pipeline.convert", fake_convert)
    cli.main(["listen", "--now", "--local-only", str(Path(__file__).parent / "fixtures" / "alice.md")])
    assert seen["llm"] == "none"


def test_cli_keys_set_saves_key_and_enables(env, monkeypatch, capsys):
    import io
    from bookbuddy import cli
    monkeypatch.setattr("sys.stdin", io.StringIO(KEY_A + "\n"))
    assert cli.main(["keys", "set", "anthropic"]) == 0
    config.reload_settings()
    st = config.get_settings()
    assert st.llm_enabled is True and st.llm_providers["anthropic"]["api_key"] == KEY_A
    assert resolve_order("auto") == ["anthropic"]
    assert KEY_A not in capsys.readouterr().out
    assert cli.main(["keys", "status"]) == 0
    out = capsys.readouterr().out
    assert KEY_A not in out and "on" in out and KEY_A[-4:] in out


def test_cli_keys_set_rejects_junk(env, monkeypatch):
    import io
    from bookbuddy import cli
    monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
    assert cli.main(["keys", "set", "openai"]) == 2
    assert config.get_settings().llm_enabled is False


# ------------------------------------------------------------------ keys never reach logs

def test_redact_masks_known_shapes_and_registered_keys():
    logs.register_secret("my-own-odd-token-value")
    s = logs.redact(f"failed with {KEY_A} and {KEY_O} and my-own-odd-token-value and Authorization: Bearer abcdefghijklmnop1234")
    assert KEY_A not in s and KEY_O not in s and "odd-token" not in s and "abcdefghijklmnop" not in s
    assert logs.redact("plain text stays") == "plain text stays"


def test_log_filter_masks_messages_args_and_tracebacks():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(logs.RedactFilter())
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("bookbuddy.test_redact")
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    lg.warning("key is %s", KEY_A)
    lg.warning(f"key is {KEY_O}")
    try:
        raise RuntimeError(f"401 for key {KEY_A}")
    except RuntimeError:
        lg.exception("call failed")
    out = buf.getvalue()
    assert KEY_A not in out and KEY_O not in out and out.count("[redacted]") >= 3


def test_provider_errors_and_log_files_never_contain_the_key(env):
    for h in list(logging.getLogger("bookbuddy").handlers):
        logging.getLogger("bookbuddy").removeHandler(h)
    logs.setup_logging(verbose=True, console=False)
    try:
        p = with_client(AnthropicProvider, anthropic_client(error=RuntimeError(f"upstream said key {KEY_A} is invalid")), api_key=KEY_A)
        c = Cleaner([p], min_score=0)
        c.clean_chunk(TEXT, force=True)
        with pytest.raises(ProviderError) as e:
            p.run("x")
        assert KEY_A not in str(e.value)
        for h in logging.getLogger("bookbuddy").handlers:
            h.flush()
        text = (config.get_settings().log_dir / "bookbuddy.log").read_text()
        assert "provider anthropic" in text.lower() or "anthropic" in text
        assert KEY_A not in text
    finally:
        for h in list(logging.getLogger("bookbuddy").handlers):
            logging.getLogger("bookbuddy").removeHandler(h)
            h.close()
