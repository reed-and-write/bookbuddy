"""Plugin registries.

Four extension points, each a `Registry`:

    extractors     group `bookbuddy.extractors`     module/object with can_handle(path) and extract(path, opts) -> Book
    engines        group `bookbuddy.engines`        class(voice, speed, device) with .synth(text) -> float32 ndarray
    notifiers      group `bookbuddy.notifiers`      class(settings, outbox=, dry_run=) with .send(kind, title, message, status)
    llm_providers  group `bookbuddy.llm_providers`  llm_clean.Provider subclass

Built-ins are registered in code. Third-party plugins arrive through setuptools entry points in the group above,
or as drop-in files in `<config dir>/plugins/*.py` that define any of the module-level dicts EXTRACTORS, ENGINES,
NOTIFIERS, LLM_PROVIDERS (name -> object). A broken plugin is logged and skipped; it never stops Book Buddy.
Plugin files are ordinary Python executed with your privileges: only put files there that you trust.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("bookbuddy.registry")

_PLUGIN_ATTRS = {"extractor": "EXTRACTORS", "engine": "ENGINES", "notifier": "NOTIFIERS",
                 "LLM provider": "LLM_PROVIDERS"}


class Registry:
    def __init__(self, kind: str, group: str):
        self.kind = kind
        self.group = group
        self._builtin: dict[str, Any] = {}
        self._plugins: dict[str, Any] = {}
        self._loaded = False

    # -- registration
    def register(self, name: str, obj: Any, *, builtin: bool = False) -> None:
        (self._builtin if builtin else self._plugins)[name] = obj

    def unregister(self, name: str) -> None:
        self._builtin.pop(name, None)
        self._plugins.pop(name, None)

    # -- loading third-party plugins (once per process, or after reset())
    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            eps = importlib.metadata.entry_points(group=self.group)
        except Exception as e:  # noqa: BLE001
            log.warning("could not read entry points for %s: %s", self.group, e)
            eps = []
        for ep in eps:
            try:
                self._plugins.setdefault(ep.name, ep.load())
            except Exception as e:  # noqa: BLE001 - a broken plugin must not break the app
                log.warning("%s plugin %r (%s) failed to load: %s", self.kind, ep.name, ep.value, e)
        for name, obj in plugin_dir_objects(self.kind).items():
            self._plugins.setdefault(name, obj)

    def reset(self) -> None:
        """Forget third-party plugins so the next access reloads them (tests, config changes)."""
        self._plugins.clear()
        self._loaded = False

    # -- lookup
    def names(self) -> list[str]:
        self.load()
        return sorted({*self._builtin, *self._plugins})

    def get(self, name: str) -> Any:
        self.load()
        if name in self._plugins:
            return self._plugins[name]
        if name in self._builtin:
            return self._builtin[name]
        raise KeyError(f"no {self.kind} named {name!r} (available: {', '.join(self.names()) or 'none'})")

    def has(self, name: str) -> bool:
        self.load()
        return name in self._plugins or name in self._builtin

    def items(self) -> Iterator[tuple[str, Any]]:
        """Third-party plugins first (they may override a built-in), then built-ins; each group by name."""
        self.load()
        seen = set()
        for src in (self._plugins, self._builtin):
            for name in sorted(src):
                if name not in seen:
                    seen.add(name)
                    yield name, src[name]




def plugin_dir_objects(kind: str) -> dict[str, Any]:
    """Objects of one kind declared by the drop-in files in the plugins directory."""
    from . import config
    d = config.plugins_dir()
    if not d.is_dir():
        return {}
    attr = _PLUGIN_ATTRS[kind]
    out: dict[str, Any] = {}
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        modname = f"bookbuddy_plugin_{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(modname, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[modname] = mod              # so the file can refer to itself (sys.modules[__name__])
            spec.loader.exec_module(mod)           # type: ignore[union-attr]
        except (Exception, SystemExit) as e:  # noqa: BLE001
            sys.modules.pop(modname, None)
            log.warning("plugin file %s failed to load: %r", f, e)
            continue
        found = getattr(mod, attr, None)
        if isinstance(found, dict):
            out.update(found)
    return out


extractors = Registry("extractor", "bookbuddy.extractors")
engines = Registry("engine", "bookbuddy.engines")
notifiers = Registry("notifier", "bookbuddy.notifiers")
llm_providers = Registry("LLM provider", "bookbuddy.llm_providers")

ALL = (extractors, engines, notifiers, llm_providers)


def reset_all() -> None:
    for r in ALL:
        r.reset()


def ensure_builtins() -> None:
    """Import the modules that register the built-in plugins (idempotent)."""
    from . import engines as _e, extractors as _x, llm_providers as _l, notifiers as _n  # noqa: F401
