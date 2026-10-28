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
