from __future__ import annotations

import pytest

from app.services.romanize import contains_devanagari, romanize


@pytest.mark.parametrize(
    "hindi,roman",
    [
        ("शैतान के सामने चुनौती", "Shaitaan ke saamne chunauti"),  # silent schwa: saamne
        ("जिंदा बचकर कोई नहीं जाएगा", "Jinda bachkar koi nahin jaaega"),
        ("यह क्या है? मैं नहीं जानता।", "Yah kya hai? Main nahin jaanta."),
        ("हिंदी संपर्क गया था", "Hindi sampark gaya tha"),  # n before most letters, m before p
        ("सत्य और रहस्य", "Satya aur rahasya"),  # Sanskrit endings keep the final a
        ("ज्ञान का क्षेत्र", "Gyaan ka kshetra"),
        ("दोस्त धर्म", "Dost dharm"),  # Hindi endings lose it
        ("वैटिकन की गुहा में रहस्य", "Vaitikan ki guha mein rahasya"),
    ],
)
def test_romanize_hindi_into_natural_hinglish(hindi, roman):
    assert romanize(hindi) == roman


def test_romanize_leaves_other_text_alone_and_handles_digits():
    assert romanize("The Popes Exorcist की कहानी") == "The Popes Exorcist ki kahaani"
    assert romanize("#कहानी") == "#kahaani"
    assert romanize("१२ बजे") == "12 baje"
    assert romanize("Already roman 123") == "Already roman 123"
    assert romanize("") == ""


def test_contains_devanagari():
    assert contains_devanagari("कहानी") and contains_devanagari("a कहानी b")
    assert not contains_devanagari("kahani")
