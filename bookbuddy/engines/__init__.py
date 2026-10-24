"""TTS engines. An engine is a class `Engine(voice, speed, device)` with `sample_rate` and
`synth(text) -> float32 numpy array` (mono). Built-ins: `kokoro` (default) and `fake` (tests/CI: a tone).
Third-party engines register in the `bookbuddy.engines` entry-point group."""
from __future__ import annotations

from .. import config
from ..registry import engines as registry
from .fake import FakeEngine
from .kokoro import KokoroEngine

registry.register("kokoro", KokoroEngine, builtin=True)
registry.register("fake", FakeEngine, builtin=True)


def make_engine(voice: str, speed: float, device: str | None = None, name: str | None = None):
    name = name or config.get_settings().engine
    return registry.get(name)(voice, speed, device)
