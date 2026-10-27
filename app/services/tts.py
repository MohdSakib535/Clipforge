"""Voice-over for reels: Kokoro v1.0 (82M parameters, ONNX int8) on the CPU.

In the worker container it reads Hindi ~4x and English ~7x faster than real
time, so a 35-second reel is voiced in well under 10 seconds. The model (~120
MB with the voices) downloads on first use to ``<storage>/.kokoro``.

kokoro-onnx and numpy are imported lazily: the API only needs ``LANGUAGES``.
"""

from __future__ import annotations

import os
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
    voices: dict[str, str] = field(default_factory=dict)  # Kokoro voice -> label
    # Kokoro's speaking rate (measured), used to size the script to the length.
    words_per_s: float = 2.8
    caption_font: str = "Noto Sans"


# Only languages Kokoro reads well through espeak-ng. Japanese and Chinese need
# extra text processing that the ONNX build doesn't do, so they are left out.
LANGUAGES: dict[str, Language] = {
    "hi": Language(
        "Hindi",
        "Hindi, written in Devanagari script",
        "hi",
        {"hf_alpha": "Female 1", "hf_beta": "Female 2", "hm_omega": "Male 1", "hm_psi": "Male 2"},
        words_per_s=2.5,
        caption_font="Noto Sans Devanagari",
    ),
    "en": Language(
        "English",
        "English",
        "en-us",
        {"af_heart": "Female 1", "af_bella": "Female 2",
         "am_michael": "Male 1", "am_fenrir": "Male 2"},
        words_per_s=3.0,
    ),
    "es": Language("Spanish", "Spanish", "es", {"ef_dora": "Female", "em_alex": "Male"}),
    "fr": Language("French", "French", "fr-fr", {"ff_siwis": "Female"}),
    "it": Language("Italian", "Italian", "it", {"if_sara": "Female", "im_nicola": "Male"}),
    "pt": Language(
        "Portuguese (Brazil)", "Brazilian Portuguese", "pt-br",
        {"pf_dora": "Female", "pm_alex": "Male"},
    ),
}  # fmt: skip


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


def synthesize(text: str, language: str, voice: str):
    """Speak ``text``. Returns (mono float32 samples, sample rate)."""
    lang = LANGUAGES.get(language)
    if lang is None or voice not in lang.voices:
        raise TTSError(f"Unknown voice {voice!r} for language {language!r}")
    try:
        return _get_kokoro().create(text, voice=voice, speed=1.0, lang=lang.kokoro_lang)
    except TTSError:
        raise
    except Exception as exc:
        raise TTSError(f"Voice-over failed: {exc}") from exc
