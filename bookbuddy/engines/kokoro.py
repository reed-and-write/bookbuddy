"""Kokoro TTS (82M parameters, Apache-2.0). Runs on CUDA when available, else CPU."""
from __future__ import annotations

import logging

import numpy as np

from .. import config

log = logging.getLogger("bookbuddy.engines.kokoro")


class KokoroEngine:
    sample_rate = config.SAMPLE_RATE

    def __init__(self, voice: str = config.DEFAULT_VOICE, speed: float = 1.0, device: str | None = None):
        config.setup_env()
        import torch
        from kokoro import KPipeline

        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cpu":
            log.warning("CUDA not available; running Kokoro on CPU (slow)")
        self.device = device
        self.voice = voice
        self.speed = speed
        lang = voice[0] if voice and voice[0] in "abefhijpz" else "a"
        log.info("loading Kokoro (voice=%s speed=%s device=%s)", voice, speed, device)
        self.pipeline = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M", device=device)

    def synth(self, text: str) -> np.ndarray:
        parts = []
        for result in self.pipeline(text, voice=self.voice, speed=self.speed):
            audio = result.audio
            if audio is None:
                continue
            parts.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio))
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts).astype(np.float32)
