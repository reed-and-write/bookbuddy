"""Kokoro voices with friendly names (English voices; other languages need extra Kokoro packages)."""
from __future__ import annotations

VOICES: dict[str, dict] = {}

# Short style words, measured from a ~13 s sample of each voice at 1.0x (not hand-picked): pitch vs the other voices
# of the same gender (light / low or deep), spectral brightness (bright / warm), pitch movement (lively / calm) and
# pace (unhurried / quick). Up to two words per voice, the strongest differences only; "balanced" when none stands out.
STYLE: dict[str, str] = {
    "af_alloy": "low, warm",
    "af_aoede": "warm",
    "af_bella": "light, calm",
    "af_heart": "light, bright",
    "af_jessica": "light, quick",
    "af_kore": "low, lively",
    "af_nicole": "bright, unhurried",
    "af_nova": "warm, calm",
    "af_river": "bright, quick",
    "af_sarah": "bright, lively",
    "af_sky": "low, warm",
    "am_adam": "balanced",
    "am_echo": "deep, warm",
    "am_eric": "light, quick",
    "am_fenrir": "lively",
    "am_liam": "quick",
    "am_michael": "lively, unhurried",
    "am_onyx": "deep, warm",
    "am_puck": "deep, lively",
    "am_santa": "light, lively",
    "bf_alice": "light, lively",
    "bf_emma": "bright, calm",
    "bf_isabella": "light, bright",
    "bf_lily": "balanced",
    "bm_daniel": "calm, quick",
    "bm_fable": "warm, quick",
    "bm_george": "light, calm",
    "bm_lewis": "deep, unhurried",
}


def _add(vid: str, name: str, accent: str, gender: str) -> None:
    style = STYLE.get(vid, "")
    VOICES[vid] = {"id": vid, "name": name, "accent": accent, "gender": gender, "style": style,
                   "label": f"{name} ({style + ', ' if style else ''}{accent}, {gender})"}


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
