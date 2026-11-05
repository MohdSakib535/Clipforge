"""Voice-over for reels: Kokoro v1.0 (82M parameters, ONNX int8) on the CPU.

In the worker container it reads Hindi ~4x and English ~7x faster than real
time, so a 35-second reel is voiced in well under 10 seconds. The model (~120
MB with the voices) downloads on first use to ``<storage>/.kokoro``.

kokoro-onnx and numpy are imported lazily: the API only needs ``LANGUAGES``.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import threading
import unicodedata
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
         "hf_husky": "Female 7 · husky", "hf_mature": "Female 8 · mature",
         "hf_girl": "Female 9 · young girl", "hf_story": "Female 10 · storyteller",
         "hf_deep": "Female 11 · deep", "hf_gentle": "Female 12 · gentle",
         "hm_omega": "Male 1", "hm_psi": "Male 2", "hm_mix": "Male 3 · mixed",
         "hm_deep": "Male 4 · deep", "hm_calm": "Male 5 · calm", "hm_young": "Male 6 · young",
         "hm_soft": "Male 7 · soft", "hm_teen": "Male 8 · teen",
         "hm_bass": "Male 9 · very deep", "hm_narrator": "Male 10 · narrator",
         "hm_elder": "Male 11 · elder", "hm_energetic": "Male 12 · energetic"},
        words_per_s=2.5,
        # Measured 2026-11-02 on 585 words of real reel narrations, read as
        # reels read them (by_sentence, pauses included): the female voices
        # read up to ~20% faster than the male ones. Voices 7-12 (2026-11-05):
        # measured on a 211-word narration, scaled by their base voices' ratio
        # of the two measurements (checked on hf_mix/hm_mix: within 1%); a
        # pitch-only voice reads at its base voice's pace.
        voice_words_per_s={
            "hf_alpha": 2.59, "hf_beta": 2.93, "hf_mix": 2.74, "hf_low": 2.94,
            "hf_calm": 2.40, "hf_bright": 2.73,
            "hf_husky": 2.84, "hf_mature": 2.58, "hf_girl": 3.02, "hf_story": 2.70,
            "hf_deep": 2.59, "hf_gentle": 2.63,
            "hm_omega": 2.51, "hm_psi": 2.52, "hm_mix": 2.51, "hm_deep": 2.51,
            "hm_calm": 2.34, "hm_young": 2.66,
            "hm_soft": 2.65, "hm_teen": 2.65, "hm_bass": 2.52, "hm_narrator": 2.36,
            "hm_elder": 2.20, "hm_energetic": 2.68,
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
    accent stays: a blend of two of them (``share`` of ``blend``, 50/50 by
    default) and/or a small change of pitch (semitones; the voice's formants
    are kept, so it still sounds natural) and of pace (Kokoro's speed)."""

    base: str
    blend: str | None = None
    pitch: float = 0.0
    speed: float = 1.0
    share: float = 0.5  # how much of ``blend`` is in the voice


# Kokoro has only two Hindi voices of each kind; these make more types from
# them. Blending in an English voice brings its accent (tried 2026-11-05), so
# only the 4 Hindi voices are used; some blend a little of the other kind.
# Measured pitch: hf_alpha ~203 Hz, hf_beta ~182, hm_omega ~124, hm_psi ~131.
MIXES: dict[str, VoiceMix] = {
    "hf_mix": VoiceMix("hf_alpha", blend="hf_beta"),
    "hf_low": VoiceMix("hf_beta", pitch=-1.5),
    "hf_calm": VoiceMix("hf_alpha", pitch=-0.5, speed=0.9),
    "hf_bright": VoiceMix("hf_alpha", pitch=1.5, speed=1.08),
    "hf_husky": VoiceMix("hf_beta", blend="hm_omega", share=0.25),
    "hf_mature": VoiceMix("hf_alpha", blend="hm_psi", share=0.3, pitch=-1.0),
    "hf_girl": VoiceMix("hf_beta", pitch=2.5, speed=1.05),
    "hf_story": VoiceMix("hf_beta", pitch=-0.5, speed=0.88),
    "hf_deep": VoiceMix("hf_alpha", pitch=-2.0),
    "hf_gentle": VoiceMix("hf_alpha", blend="hf_beta", share=0.4, pitch=-1.0, speed=0.95),
    "hm_mix": VoiceMix("hm_omega", blend="hm_psi"),
    "hm_deep": VoiceMix("hm_omega", pitch=-3.0),
    "hm_calm": VoiceMix("hm_psi", pitch=-1.0, speed=0.9),
    "hm_young": VoiceMix("hm_psi", pitch=1.5, speed=1.08),
    "hm_soft": VoiceMix("hm_psi", blend="hf_beta", share=0.25),
    "hm_teen": VoiceMix("hm_psi", blend="hf_alpha", share=0.35, pitch=1.0, speed=1.05),
    "hm_bass": VoiceMix("hm_psi", pitch=-3.5),
    "hm_narrator": VoiceMix("hm_omega", blend="hm_psi", share=0.3, pitch=-1.5, speed=0.92),
    "hm_elder": VoiceMix("hm_omega", pitch=-2.0, speed=0.85),
    "hm_energetic": VoiceMix("hm_omega", pitch=1.0, speed=1.1),
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


# Raised when text is read differently (the Hindi fixes, one sentence at a
# time), so voice clips made before are made again.
READING = 3


def voice_key(voice: str) -> str:
    """What makes the voice, for naming cached voice clips: changing a mix,
    or how text is read, makes its clips again."""
    mix = MIXES.get(voice)
    if mix is None:
        return f"{voice}#{READING}"
    blend = mix.blend if mix.share == 0.5 else f"{mix.blend}*{mix.share}"
    return f"{voice}={mix.base}+{blend}@{mix.pitch}x{mix.speed}#{READING}"


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


# ---------------------------------------------------------------------------
# Hindi: what espeak-ng reads wrong
# ---------------------------------------------------------------------------
# Kokoro learnt Hindi from espeak-ng's sounds (through misaki, which gives the
# same sounds as _phonemes), so espeak's mistakes are fixed in the text it
# gets; the captions keep the text as written.

_VIRAMA, _NUKTA = "्", "़"
_CHANDRABINDU = "ँ"
_SIGNS = "ँंः"  # chandrabindu, anusvara, visarga
_HINDI_WORD = re.compile(r"[ऀ-ॣॱ-ॿ]+")  # not ।, ॥ or digits
_VOWEL_SOUNDS = frozenset("aʌəɛeiɪoɔuʊæɐ")  # in espeak's Hindi sounds


def _is_consonant(ch: str) -> bool:
    return "क" <= ch <= "ह" or "क़" <= ch <= "य़"


def _is_vowel_sign(ch: str) -> bool:
    return "ऺ" <= ch <= "ौ" or ch in "ॎॏॢॣ"


def _is_vowel(ch: str) -> bool:
    return "ऄ" <= ch <= "औ" or ch in "ॠॡ"


@dataclass
class _Akshara:
    """A written syllable: a vowel, or a consonant with its vowel sign or virama."""

    text: str
    consonant: bool = False
    inherent: bool = False  # a consonant with its own "a" (no vowel sign, no virama)
    virama: bool = False
    coda: bool = False  # an anusvara or visarga after it: it ends in a consonant sound


def _aksharas(word: str) -> list[_Akshara] | None:
    """``word`` in written syllables; None if it has anything else in it."""
    out, i = [], 0
    while i < len(word):
        ch = word[i]
        i += 1
        if _is_vowel(ch):
            syl = _Akshara(ch)
        elif _is_consonant(ch):
            syl = _Akshara(ch, consonant=True)
            if word[i : i + 1] == _NUKTA:
                syl.text += _NUKTA
                i += 1
            if word[i : i + 1] == _VIRAMA:
                syl.text, syl.virama = syl.text + _VIRAMA, True
                i += 1
            elif i < len(word) and _is_vowel_sign(word[i]):
                syl.text += word[i]
                i += 1
            else:
                syl.inherent = True
        else:
            return None
        while i < len(word) and word[i] in _SIGNS:
            syl.coda |= word[i] != _CHANDRABINDU  # which only makes the vowel nasal
            syl.text += word[i]
            i += 1
        out.append(syl)
    return out


def silent_schwas(word: str) -> tuple[str, int] | None:
    """``word`` with a virama after each consonant whose "a" Hindi doesn't say,
    and how many vowels are then said; None if every "a" is said. Ohala's rule,
    applied right to left: the "a" between a vowel + consonant and a consonant
    + vowel drops (बचपन "bach-pan", देखकर "dekh-kar", समझना "sa-majh-na");
    a word's last "a" is never said."""
    syl = _aksharas(word)
    if not syl or len(syl) < 3:
        return None
    last = len(syl) - 1
    dropped: set[int] = set()

    def said(i: int) -> bool:  # syllable i has a vowel that is said
        s = syl[i]
        return not s.virama and not (s.inherent and not s.coda and (i == last or i in dropped))

    def ends_in_vowel(i: int) -> bool:
        return said(i) and not syl[i].coda

    for i in range(last - 1, 0, -1):
        s, after = syl[i], syl[i + 1]
        if s.inherent and not s.coda and ends_in_vowel(i - 1) and after.consonant and said(i + 1):
            dropped.add(i)
    if not dropped:
        return None
    marked = "".join(s.text + _VIRAMA * (i in dropped) for i, s in enumerate(syl))
    return marked, sum(said(i) for i in range(len(syl)))


# English words written in Hindi: an f is written फ, which espeak reads "ph"
# (फॉलो "pholo"); ॉ and ऑ come only in English words.
_ENGLISH_F_WORDS = frozenset(
    "फोन फोटो फोटोज़ फेसबुक फिल्म फिल्में फ्री फ्रेंड फ्रेंड्स फैमिली फैन फैंस फेमस "
    "फिट फीचर फ्यूचर फैक्ट फैशन फ्लाइट फैक्ट्री लाइफ सेफ स्टाफ".split()
)
_PH = re.compile(f"फ(?!{_NUKTA})")


def _english_f(word: str) -> str:
    if "ॉ" in word or "ऑ" in word or word in _ENGLISH_F_WORDS:
        return _PH.sub("फ" + _NUKTA, word)
    return word


# Words espeak reads wrong whatever the rule says, as it is given them.
_SPELLINGS = {
    "ज़िंदगी": "ज़िन्दगी",  # "jind-gi": espeak drops the "a" after the anusvara
    "इंतज़ार": "इन्तज़ार",  # "in-tjaar"
    "अजनबी": "अज्नबी",  # "a-jan-bi"
}
# Words said with a z or f (ज़, फ़) that scripts often write without the dot;
# not the ones that mean something else without it (राज/राज़, सजा/सज़ा).
_DOTTED = (
    "ज़रूर ज़रूरी ज़्यादा आवाज़ बाज़ार मज़ा मज़ेदार अंदाज़ नज़र नज़दीक ज़मीन ज़माना ज़हर कमज़ोर "
    "ज़ोर ज़िद ज़िंदा ज़िम्मेदार ज़िम्मेदारी हज़ार गुज़र गुज़रा इज़्ज़त सफ़र फ़ैसला साफ़ माफ़ हफ़्ता "
    "फ़िक्र तरफ़ फ़ायदा फ़ायदे दफ़्तर तारीफ़"
).split()


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


_AS_SAID = {
    key: _nfc(said)
    for written, said in [*_SPELLINGS.items(), *((w, w) for w in _DOTTED)]
    for key in (_nfc(written), _nfc(written).replace(_NUKTA, ""))
}

# Two words made one (ताज + महल): the "a" between them is dropped and each part
# follows the rule on its own (on the whole word it gives "taa-jam-hal").
_SECOND_PARTS = ("महल", "नगर", "परी")
# अ- ("not") + a word: the rule would drop the "a" after the अ ("as-marth").
_READ_BY_ESPEAK = frozenset({"असमर्थ", "असफल", "असफलता", "असहज"})


_hi_espeak = None


def _espeak_hindi(words: list[str]) -> list[str]:
    """espeak-ng's sounds for each Hindi word (after _get_kokoro set it up)."""
    global _hi_espeak
    from phonemizer.backend import EspeakBackend

    with _espeak_lock:
        if _hi_espeak is None:
            _hi_espeak = EspeakBackend("hi", language_switch="remove-flags")
        return _hi_espeak.phonemize(words, strip=True)


def _vowels(sounds: str) -> int:
    return sum(ch in _VOWEL_SOUNDS for ch in sounds)


@functools.lru_cache(maxsize=20000)
def _hindi_word(word: str) -> str:
    """How espeak-ng is given ``word``. Its silent "a"s are marked only where
    espeak says them, and only if it then says exactly the vowels it should:
    the marks can make espeak drop another "a" (खतरनाक "khatrnak")."""
    if word in _AS_SAID:
        return _AS_SAID[word]
    if word.endswith("ंदगी"):  # गंदगी, शर्मिंदगी: "-n-da-gi", as ज़िंदगी
        word = word[: -len("ंदगी")] + "न्दगी"
    word = _english_f(word)
    if word in _READ_BY_ESPEAK:
        return word
    for part in _SECOND_PARTS:
        head = word[: -len(part)]
        if word.endswith(part) and len(head) >= 2 and _is_consonant(head[-1]):
            return _hindi_word(head) + _VIRAMA + _hindi_word(part)
    fix = silent_schwas(word)
    if fix is None:
        return word
    marked, vowels = fix
    before, after = _espeak_hindi([word, marked])
    if _vowels(before) > vowels and _vowels(after) == vowels:
        return marked
    return word


# Kokoro reads a long text faster than a short one (a 55-word scene came out
# ~11% faster than its sentences read one by one, measured), so a reel sped up
# in its long scenes. ``by_sentence`` reads them one by one, with these pauses.
SENTENCE_GAP_S = 0.3
CLAUSE_GAP_S = 0.15
_MAX_PIECE_WORDS = 24  # a longer sentence is cut at its commas
_SENTENCE_MARKS = "।॥.!?…"
_SENTENCE_END = re.compile(rf"(?<=[{_SENTENCE_MARKS}])[\"”’']?\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:])\s+")


def sentences(text: str) -> list[str]:
    """``text`` in the pieces read one by one: its sentences, a long one cut
    at its commas (into pieces of at most _MAX_PIECE_WORDS words where it can)."""
    pieces = []
    for sentence in _SENTENCE_END.split(text.strip()):
        long = len(sentence.split()) > _MAX_PIECE_WORDS
        piece = ""
        for clause in _CLAUSE_END.split(sentence) if long else [sentence]:
            joined = f"{piece} {clause}".strip()
            if piece and len(joined.split()) > _MAX_PIECE_WORDS:
                pieces.append(piece)
                joined = clause
            piece = joined
        pieces.append(piece)
    return [p for p in pieces if any(ch.isalnum() for ch in p)]


def _gap_after(piece: str) -> float:
    return SENTENCE_GAP_S if piece.rstrip("\"”’'")[-1:] in _SENTENCE_MARKS else CLAUSE_GAP_S


def hindi_for_espeak(text: str) -> str:
    """``text`` as espeak-ng reads it right."""
    # NFC: a dotted letter typed as one character (U+095B) is ज + ़ after it.
    return _HINDI_WORD.sub(lambda m: _hindi_word(m.group()), _nfc(text))


def synthesize(
    text: str, language: str, voice: str, speed: float = 1.0, *, by_sentence: bool = False
):
    """Speak ``text`` (``speed`` > 1 reads faster); ``by_sentence``: one
    sentence at a time, at an even pace however long the text. Returns (mono
    float32 samples, sample rate)."""
    import numpy as np

    lang = LANGUAGES.get(language)
    if lang is None or voice not in lang.voices:
        raise TTSError(f"Unknown voice {voice!r} for language {language!r}")
    mix = MIXES.get(voice)
    try:
        kokoro = _get_kokoro()  # also points espeak-ng at its library
        style = voice
        if mix is not None:
            style = kokoro.get_voice_style(mix.base)
            if mix.blend:  # 50/50 gives exactly (a + b) / 2, as before
                blend = kokoro.get_voice_style(mix.blend)
                style = style * (1 - mix.share) + blend * mix.share
            speed *= mix.speed
        if language == "hi":
            text = hindi_for_espeak(text)
        pieces = (sentences(text) if by_sentence else None) or [text]
        parts = []
        for i, piece in enumerate(pieces):
            samples, sample_rate = kokoro.create(
                _phonemes(kokoro, piece, lang.kokoro_lang),
                voice=style,
                speed=speed,
                lang=lang.kokoro_lang,
                is_phonemes=True,
            )
            parts.append(samples)
            if i < len(pieces) - 1:
                gap = round(_gap_after(piece) / speed * sample_rate)
                parts.append(np.zeros(gap, np.float32))
        samples = np.concatenate(parts) if len(parts) > 1 else parts[0]
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
