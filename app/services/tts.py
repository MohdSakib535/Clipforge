"""Voice-over for reels: Kokoro v1.0 (82M parameters, ONNX int8) on the CPU.

In the worker container it reads Hindi ~4x and English ~7x faster than real
time, so a 35-second reel is voiced in well under 10 seconds. The model (~120
MB with the voices) downloads on first use to ``<storage>/.kokoro``.

kokoro-onnx and numpy are imported lazily: the API only needs ``LANGUAGES``.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config.settings import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class Language:
    label: str  # shown in the UI
    prompt_name: str  # how the script writer is told which language to write
    kokoro_lang: str  # espeak-ng code Kokoro turns the text into sounds with
    voices: dict[str, str] = field(default_factory=dict)  # Kokoro voice or MIXES key -> label
    # Kokoro's speaking rate (measured), used to size the script to the length;
    # voices that were measured one by one have theirs in voice_words_per_s.
    words_per_s: float = 2.8
    voice_words_per_s: dict[str, float] = field(default_factory=dict)
    caption_font: str = "Noto Sans"
    sample: str = ""  # read out by the "Listen" button next to the voices


# Only languages Kokoro reads well through espeak-ng. Japanese and Chinese need
# extra text processing that the ONNX build doesn't do, so they are left out.
LANGUAGES: dict[str, Language] = {
    "hi": Language(
        "Hindi",
        "Hindi, written in Devanagari script",
        "hi",
        {"hf_alpha": "Female 1", "hf_beta": "Female 2", "hf_mix": "Female 3 · mixed",
         "hf_low": "Female 4 · soft", "hf_calm": "Female 5 · calm",
         "hf_bright": "Female 6 · bright",
         "hm_omega": "Male 1", "hm_psi": "Male 2", "hm_mix": "Male 3 · mixed",
         "hm_deep": "Male 4 · deep", "hm_calm": "Male 5 · calm", "hm_young": "Male 6 · young"},
        words_per_s=2.5,
        # Measured 2026-11-02 on 585 words of real reel narrations: the female
        # voices read up to ~20% faster than the male ones.
        voice_words_per_s={
            "hf_alpha": 2.68, "hf_beta": 3.02, "hf_mix": 2.83, "hf_low": 3.03,
            "hf_calm": 2.50, "hf_bright": 2.82,
            "hm_omega": 2.55, "hm_psi": 2.54, "hm_mix": 2.54, "hm_deep": 2.56,
            "hm_calm": 2.37, "hm_young": 2.68,
        },
        caption_font="Noto Sans Devanagari",
        sample="नमस्ते! यह आपकी रील की आवाज़ है। सुनिए, आज की कहानी बहुत खास है।",
    ),
    "en": Language(
        "English",
        "English",
        "en-us",
        {"af_heart": "Female 1", "af_bella": "Female 2",
         "am_michael": "Male 1", "am_fenrir": "Male 2"},
        words_per_s=3.0,
        sample="Hi! This is the voice of your reel. Listen, today's story is a special one.",
    ),
    "es": Language(
        "Spanish", "Spanish", "es", {"ef_dora": "Female", "em_alex": "Male"},
        sample="¡Hola! Esta es la voz de tu reel. Escucha, la historia de hoy es muy especial.",
    ),
    "fr": Language(
        "French", "French", "fr-fr", {"ff_siwis": "Female"},
        sample="Bonjour ! Voici la voix de votre reel. Écoutez, l'histoire d'aujourd'hui est "
        "très spéciale.",
    ),
    "it": Language(
        "Italian", "Italian", "it", {"if_sara": "Female", "im_nicola": "Male"},
        sample="Ciao! Questa è la voce del tuo reel. Ascolta, la storia di oggi è davvero "
        "speciale.",
    ),
    "pt": Language(
        "Portuguese (Brazil)", "Brazilian Portuguese", "pt-br",
        {"pf_dora": "Female", "pm_alex": "Male"},
        sample="Olá! Esta é a voz do seu reel. Escute, a história de hoje é muito especial.",
    ),
}  # fmt: skip


@dataclass(frozen=True)
class VoiceMix:
    """A voice made from Kokoro's own voices of the same language, so the
    accent stays: a 50/50 blend of two of them and/or a small change of pitch
    (semitones; the voice's formants are kept, so it still sounds natural) and
    of pace (Kokoro's speed)."""

    base: str
    blend: str | None = None
    pitch: float = 0.0
    speed: float = 1.0


# Kokoro has only two Hindi voices of each kind; these make more types from
# them. Measured pitch: hf_alpha ~203 Hz, hf_beta ~182, hm_omega ~124, hm_psi ~131.
MIXES: dict[str, VoiceMix] = {
    "hf_mix": VoiceMix("hf_alpha", blend="hf_beta"),
    "hf_low": VoiceMix("hf_beta", pitch=-1.5),
    "hf_calm": VoiceMix("hf_alpha", pitch=-0.5, speed=0.9),
    "hf_bright": VoiceMix("hf_alpha", pitch=1.5, speed=1.08),
    "hm_mix": VoiceMix("hm_omega", blend="hm_psi"),
    "hm_deep": VoiceMix("hm_omega", pitch=-3.0),
    "hm_calm": VoiceMix("hm_psi", pitch=-1.0, speed=0.9),
    "hm_young": VoiceMix("hm_psi", pitch=1.5, speed=1.08),
}


def voice_speed(voice: str | None) -> float:
    """How fast ``voice`` speaks compared with its language's usual pace."""
    mix = MIXES.get(voice or "")
    return mix.speed if mix else 1.0


def words_per_s(language: str, voice: str | None = None) -> float:
    """How many words a second ``voice`` reads at its usual pace."""
    lang = LANGUAGES[language]
    if voice in lang.voice_words_per_s:
        return lang.voice_words_per_s[voice]
    return lang.words_per_s * voice_speed(voice)


def voice_key(voice: str) -> str:
    """What makes the voice, for naming cached voice clips: changing a mix
    makes its clips again."""
    mix = MIXES.get(voice)
    return f"{voice}={mix.base}+{mix.blend}@{mix.pitch}x{mix.speed}" if mix else voice


def default_voice(language: str) -> str:
    return next(iter(LANGUAGES[language].voices))


_kokoro = None
_lock = threading.Lock()


def _download(url: str, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    os.replace(tmp, dst)


def _get_kokoro():
    global _kokoro
    with _lock:
        if _kokoro is None:
            settings = get_settings()
            model_dir = Path(settings.storage_path) / ".kokoro"
            model_dir.mkdir(parents=True, exist_ok=True)
            files = []
            for url in (settings.kokoro_model_url, settings.kokoro_voices_url):
                path = model_dir / Path(urlparse(url).path).name
                if not path.exists():
                    log.info("kokoro_download", extra={"file": path.name})
                    try:
                        _download(url, path)
                    except httpx.HTTPError as exc:
                        raise TTSError(f"Could not download the voice model: {exc}") from exc
                files.append(str(path))
            from kokoro_onnx import Kokoro

            _kokoro = Kokoro(*files)
        return _kokoro


_espeak_lock = threading.Lock()  # espeak-ng keeps global state: one text at a time


def _phonemes(kokoro, text: str, espeak_lang: str) -> str:
    """The sounds Kokoro reads for ``text``. Kokoro's own conversion keeps
    espeak's language-switch marks, so an English word in a Hindi line
    ("Garden Show में") becomes "(en)ɡˈɑːdən ʃˈəʊ(hi)" and the marks are read
    out ("in ... hee"). Dropping them reads the word with English sounds."""
    import phonemizer

    with _espeak_lock:
        phonemes = phonemizer.phonemize(
            text.strip(),
            espeak_lang,
            preserve_punctuation=True,
            with_stress=True,
            language_switch="remove-flags",
        )
    return "".join(p for p in phonemes if p in kokoro.tokenizer.vocab).strip()


def synthesize(text: str, language: str, voice: str, speed: float = 1.0):
    """Speak ``text`` (``speed`` > 1 reads faster). Returns (mono float32
    samples, sample rate)."""
    lang = LANGUAGES.get(language)
    if lang is None or voice not in lang.voices:
        raise TTSError(f"Unknown voice {voice!r} for language {language!r}")
    mix = MIXES.get(voice)
    try:
        kokoro = _get_kokoro()  # also points espeak-ng at its library
        style = voice
        if mix is not None:
            style = kokoro.get_voice_style(mix.base)
            if mix.blend:
                style = (style + kokoro.get_voice_style(mix.blend)) / 2
            speed *= mix.speed
        samples, sample_rate = kokoro.create(
            _phonemes(kokoro, text, lang.kokoro_lang),
            voice=style,
            speed=speed,
            lang=lang.kokoro_lang,
            is_phonemes=True,
        )
        if mix is not None and mix.pitch:
            samples = shift_pitch(samples, sample_rate, mix.pitch)
        return samples, sample_rate
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(f"Voice-over failed: {exc}") from exc


def pitch_cmd(sample_rate: int, semitones: float) -> list[str]:
    """FFmpeg reading and writing raw mono float samples, the pitch moved by
    ``semitones`` with the formants kept (no "chipmunk" or "monster" sound)."""
    ratio = 2 ** (semitones / 12)
    return [
        "ffmpeg", "-v", "error",
        "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-af", f"rubberband=pitch={ratio:.5f}:formant=preserved:pitchq=quality",
        "-f", "f32le", "pipe:1",
    ]  # fmt: skip


def shift_pitch(samples, sample_rate: int, semitones: float):
    import numpy as np

    proc = subprocess.run(
        pitch_cmd(sample_rate, semitones),
        input=np.asarray(samples, dtype="<f4").tobytes(),
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-300:]
        raise TTSError(f"Changing the voice's pitch failed: {tail}")
    return np.frombuffer(proc.stdout, dtype="<f4").copy()
