"""Kokoro voice: how text is turned into the sounds it reads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import tts


@pytest.fixture(scope="module")
def kokoro():
    tokenizer = pytest.importorskip("kokoro_onnx.tokenizer")
    return SimpleNamespace(tokenizer=tokenizer.Tokenizer())  # sets up espeak-ng


def test_english_words_in_hindi_are_read_without_language_marks(kokoro):
    # espeak marks them "(en)ɡˈɑːdən ʃˈəʊ(hi)"; Kokoro read the marks out.
    phonemes = tts._phonemes(kokoro, "आज हम Garden Show में हैं।", "hi")
    assert "(" not in phonemes and ")" not in phonemes
    assert "ˈɑːdən ʃˈəʊ" in phonemes.split("hˌəm", 1)[1]  # in place, English sounds


def test_a_single_language_text_is_unchanged(kokoro):
    text = "Hello there, this is a test."
    assert tts._phonemes(kokoro, text, "en-us") == kokoro.tokenizer.phonemize(text, "en-us")


def test_new_hindi_voices_are_made_from_hindi_voices():
    hindi = tts.LANGUAGES["hi"].voices
    assert sum(k.startswith("hf_") for k in hindi) == 6
    assert sum(k.startswith("hm_") for k in hindi) == 6
    for key, mix in tts.MIXES.items():
        assert key in hindi
        for kokoro_voice in filter(None, (mix.base, mix.blend)):
            # A real Hindi Kokoro voice, of the same kind: the Indian accent stays.
            assert kokoro_voice in hindi and kokoro_voice not in tts.MIXES
            assert kokoro_voice[:2] == key[:2]


def test_a_mixed_voice_is_blended_paced_and_pitched(monkeypatch):
    np = pytest.importorskip("numpy")
    styles = {"hm_omega": np.full(4, 1.0, np.float32), "hm_psi": np.full(4, 3.0, np.float32)}
    calls, shifts = [], []

    class FakeKokoro:
        def get_voice_style(self, name):
            return styles[name]

        def create(self, phonemes, *, voice, speed, lang, is_phonemes):
            calls.append((voice, speed))
            return np.zeros(240, np.float32), 24000

    monkeypatch.setattr(tts, "_get_kokoro", lambda: FakeKokoro())
    monkeypatch.setattr(tts, "_phonemes", lambda kokoro, text, lang: text)
    monkeypatch.setattr(tts, "shift_pitch", lambda s, sr, st: shifts.append(st) or s)

    tts.synthesize("नमस्ते", "hi", "hm_omega")  # a plain Kokoro voice: by name
    assert calls[-1] == ("hm_omega", 1.0) and shifts == []
    tts.synthesize("नमस्ते", "hi", "hm_mix")
    assert list(calls[-1][0]) == [2.0] * 4 and shifts == []  # 50/50 blend
    tts.synthesize("नमस्ते", "hi", "hm_calm", speed=1.1)  # dubbing asks for a speed too
    assert calls[-1][1] == pytest.approx(1.1 * 0.9) and shifts == [-1.0]
    with pytest.raises(tts.TTSError):
        tts.synthesize("hello", "en", "hm_deep")  # a Hindi voice for English


def test_voice_speed_and_key():
    assert tts.voice_speed("hm_calm") == 0.9 and tts.voice_speed("hm_omega") == 1.0
    assert tts.voice_speed(None) == 1.0
    assert tts.voice_key("hm_omega") == "hm_omega"  # existing clips keep their names
    assert tts.voice_key("hm_deep") != "hm_deep"


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
def test_pitch_shift_moves_a_tone_an_octave_up_and_keeps_its_length():
    np = pytest.importorskip("numpy")
    sr = 24000
    tone = (0.3 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
    out = tts.shift_pitch(tone, sr, 12.0)
    assert abs(len(out) - len(tone)) < sr * 0.05
    spectrum = np.abs(np.fft.rfft(out[sr // 4 : 3 * sr // 4]))
    peak_hz = np.argmax(spectrum) * sr / (sr // 2)
    assert peak_hz == pytest.approx(440, abs=15)
