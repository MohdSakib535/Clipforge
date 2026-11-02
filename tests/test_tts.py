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
    # Clips are made again when text is read differently.
    assert tts.voice_key("hm_omega") == f"hm_omega#{tts.READING}"
    assert tts.voice_key("hm_deep") != tts.voice_key("hm_omega")


def test_the_silent_a_of_hindi_words_is_marked():
    assert tts.silent_schwas("बचपन") == ("बच्पन", 2)  # "bach-pan", not "ba-cha-pan"
    assert tts.silent_schwas("देखकर") == ("देख्कर", 2)
    assert tts.silent_schwas("मेहनत") == ("मेह्नत", 2)
    assert tts.silent_schwas("समझना") == ("समझ्ना", 3)  # right to left: "sa-majh-na"
    assert tts.silent_schwas("जंगल") is None  # after an anusvara the "a" is said
    assert tts.silent_schwas("सड़क") is None  # the last "a" is silent anyway
    assert tts.silent_schwas("राम") is None
    assert tts.silent_schwas("Ram") is None


def test_hindi_is_given_to_espeak_as_it_is_said(kokoro):
    text = "बचपन की मेहनत, फॉलो करें। खतरनाक ताजमहल!"
    # खतरनाक: marking it, espeak dropped one "a" too many; ताज + महल: two words.
    assert tts.hindi_for_espeak(text) == "बच्पन की मेह्नत, फ़ॉलो करें। खतरनाक ताज्महल!"
    assert "bˈʌcpən" in tts._phonemes(kokoro, tts.hindi_for_espeak("बचपन"), "hi")
    for word in ("अपना", "कमरा", "फल", "असमर्थ"):  # espeak reads them right already
        assert tts.hindi_for_espeak(word) == word
    assert tts.hindi_for_espeak("फोन") == "फ़ोन"  # an English word: "f", not "ph"


def test_words_espeak_gets_wrong_are_spelt_as_said():
    nfc = tts._nfc
    # Heard wrong in a reel: "jind-gi", "in-tjaar", "a-jan-bi".
    assert tts.hindi_for_espeak("मेरी जिंदगी") == nfc("मेरी ज़िन्दगी")
    assert tts.hindi_for_espeak("इंतजार") == nfc("इन्तज़ार")
    assert tts.hindi_for_espeak("अजनबी") == "अज्नबी"
    assert tts.hindi_for_espeak("\u095bिंदगी") == nfc("ज़िन्दगी")  # ज़ typed as one letter
    assert tts.hindi_for_espeak("गंदगी") == "गन्दगी"  # "gan-da-gi"
    assert tts.hindi_for_espeak("जरूर आवाज") == nfc("ज़रूर आवाज़")  # z, not j
    assert tts.hindi_for_espeak("राज") == "राज"  # rule; राज़ is a secret


def test_a_text_is_read_one_sentence_at_a_time():
    assert tts.sentences("राम घर गया। सीता आई! क्या हुआ? “चलो।” बस") == [
        "राम घर गया।", "सीता आई!", "क्या हुआ?", "“चलो।", "बस"
    ]  # fmt: skip
    short = "राम, सीता और लक्ष्मण वन गए।"
    assert tts.sentences(short) == [short]  # commas cut only a long sentence
    long = ", ".join(["एक दो तीन चार पाँच छह सात आठ"] * 4) + "।"
    assert [len(p.split()) for p in tts.sentences(long)] == [24, 8]
    assert tts.sentences(" … ") == []


def test_sentences_are_joined_with_a_pause(monkeypatch):
    np = pytest.importorskip("numpy")
    read = []

    class FakeKokoro:
        def create(self, phonemes, *, voice, speed, lang, is_phonemes):
            read.append(phonemes)
            return np.ones(100, np.float32), 24000

    monkeypatch.setattr(tts, "_get_kokoro", lambda: FakeKokoro())
    monkeypatch.setattr(tts, "_phonemes", lambda kokoro, text, lang: text)
    text = "One, two. Three!"
    samples, _ = tts.synthesize(text, "en", "af_heart", speed=0.5, by_sentence=True)
    assert read == ["One, two.", "Three!"]
    assert len(samples) == 200 + round(tts.SENTENCE_GAP_S / 0.5 * 24000)  # slower: longer
    assert samples[100:-100].max() == 0
    read.clear()
    tts.synthesize(text, "en", "af_heart")
    assert read == [text]  # in one go unless asked


def test_only_hindi_is_read_with_the_fixes(monkeypatch):
    np = pytest.importorskip("numpy")
    read = []

    class FakeKokoro:
        def create(self, phonemes, *, voice, speed, lang, is_phonemes):
            return np.zeros(240, np.float32), 24000

    monkeypatch.setattr(tts, "_get_kokoro", lambda: FakeKokoro())
    monkeypatch.setattr(tts, "_phonemes", lambda kokoro, text, lang: read.append(text) or text)
    monkeypatch.setattr(tts, "hindi_for_espeak", lambda text: f"<{text}>")
    tts.synthesize("बचपन", "hi", "hm_omega")
    tts.synthesize("childhood", "en", "af_heart")
    assert read == ["<बचपन>", "childhood"]


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
