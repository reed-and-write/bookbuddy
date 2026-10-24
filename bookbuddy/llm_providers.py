"""Bring-your-own-key LLM providers for the PDF/OCR cleanup pass: Anthropic (Claude) and OpenAI.

Nothing is sent anywhere unless the user supplied a key: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in the environment,
or `api_key` in `[llm.anthropic]` / `[llm.openai]` of config.toml (the web UI writes it there, mode 0600).
No key = the provider is not available = no LLM pass. Both use the vendors' official SDKs, imported lazily.
Keys are registered with the log redactor and are never part of an error message.
"""
from __future__ import annotations

import logging
import os

from .llm_clean import Provider, ProviderError
from .logs import redact, register_secret
from .registry import llm_providers as registry

log = logging.getLogger("bookbuddy.llm")

DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
MAX_OUTPUT_TOKENS = 8192


class _KeyedProvider(Provider):
    env_var = ""
    default_model = ""
    needs_key = True

    def __init__(self, **settings):
        super().__init__(**settings)
        self.api_key = self.find_key(settings)
        self.model = settings.get("model") or self.default_model
        self.timeout = float(settings.get("timeout", 240.0))
        register_secret(self.api_key)
        self._client = None

    @classmethod
    def find_key(cls, settings: dict | None = None) -> str | None:
        from . import config
        cfg = (settings if settings is not None else config.get_settings().llm_providers.get(cls.name, {})) or {}
        return (cfg.get("api_key") or os.environ.get(cls.env_var) or "").strip() or None

    @classmethod
    def is_configured(cls) -> bool:
        return cls.find_key() is not None

    def _make_client(self):                      # pragma: no cover - replaced by fakes in tests
        raise NotImplementedError

    @property
    def client(self):
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def _fail(self, e: Exception, fatal: bool) -> ProviderError:
        return ProviderError(f"{self.name}: {redact(type(e).__name__ + ': ' + str(e))[:300]}", fatal=fatal)


class AnthropicProvider(_KeyedProvider):
    name = "anthropic"
    env_var = "ANTHROPIC_API_KEY"
    default_model = DEFAULT_ANTHROPIC_MODEL
    est_seconds_per_chunk = 8.0

    def _make_client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=2)

    def run(self, prompt: str) -> str:
        if not self.api_key:
            raise ProviderError("anthropic: no API key", fatal=True)
        import anthropic
        try:
            msg = self.client.messages.create(model=self.model, max_tokens=MAX_OUTPUT_TOKENS,
                                              messages=[{"role": "user", "content": prompt}])
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            raise self._fail(e, fatal=True) from None        # bad key / no access / unknown model: stop using it this run
        except Exception as e:  # noqa: BLE001 - rate limits, timeouts, 5xx: a soft failure
            raise self._fail(e, fatal=False) from None
        return "".join(getattr(b, "text", "") for b in msg.content)


class OpenAIProvider(_KeyedProvider):
    name = "openai"
    env_var = "OPENAI_API_KEY"
    default_model = DEFAULT_OPENAI_MODEL
    est_seconds_per_chunk = 10.0

    def _make_client(self):
        import openai
        return openai.OpenAI(api_key=self.api_key, timeout=self.timeout, max_retries=2)

    def run(self, prompt: str) -> str:
        if not self.api_key:
            raise ProviderError("openai: no API key", fatal=True)
        import openai
        try:
            r = self.client.chat.completions.create(model=self.model, max_completion_tokens=MAX_OUTPUT_TOKENS,
                                                    messages=[{"role": "user", "content": prompt}])
        except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as e:
            raise self._fail(e, fatal=True) from None
        except Exception as e:  # noqa: BLE001
            raise self._fail(e, fatal=False) from None
        return r.choices[0].message.content or ""


registry.register("anthropic", AnthropicProvider, builtin=True)
registry.register("openai", OpenAIProvider, builtin=True)
