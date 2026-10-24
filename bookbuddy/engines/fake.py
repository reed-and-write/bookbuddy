"""Fake TTS engine for tests and CI: a deterministic tone, no GPU, instant."""
from __future__ import annotations

import os
import time

import numpy as np

from .. import config


class FakeEngine:
    """Deterministic tone whose length scales with text length (~15 chars/second)."""
    sample_rate = config.SAMPLE_RATE

    def __init__(self, voice: str = "fake", speed: float = 1.0, device=None):
        self.speed = speed

    def synth(self, text: str) -> np.ndarray:
        time.sleep(float(os.environ.get("BOOKBUDDY_FAKE_DELAY", "0")))
        n = int(self.sample_rate * max(0.3, len(text) / 15.0 / self.speed))
        t = np.arange(n) / self.sample_rate
        return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
