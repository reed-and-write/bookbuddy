"""Kokoro voices with friendly names (English voices; other languages need extra Kokoro packages)."""
from __future__ import annotations

VOICES: dict[str, dict] = {}


def _add(vid: str, name: str, accent: str, gender: str) -> None:
    VOICES[vid] = {"id": vid, "name": name, "accent": accent, "gender": gender,
                   "label": f"{name} ({accent}, {gender})"}


for _n in ("Alloy", "Aoede", "Bella", "Heart", "Jessica", "Kore", "Nicole", "Nova", "River", "Sarah", "Sky"):
    _add(f"af_{_n.lower()}", _n, "American", "female")
for _n in ("Adam", "Echo", "Eric", "Fenrir", "Liam", "Michael", "Onyx", "Puck", "Santa"):
    _add(f"am_{_n.lower()}", _n, "American", "male")
for _n in ("Alice", "Emma", "Isabella", "Lily"):
    _add(f"bf_{_n.lower()}", _n, "British", "female")
for _n in ("Daniel", "Fable", "George", "Lewis"):
    _add(f"bm_{_n.lower()}", _n, "British", "male")

SAMPLE_TEXT = "This is how your audiobook will sound. Press play, sit back, and listen along."


def list_voices() -> list[dict]:
    return list(VOICES.values())
