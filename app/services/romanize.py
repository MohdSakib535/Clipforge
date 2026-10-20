"""Devanagari -> Roman ("Hinglish") transliteration.

Clip titles and descriptions come from the transcript, which Whisper writes in
Devanagari for Hindi speech. This turns them into the Hinglish people type on
social media, with no extra dependency. It spells words the way people type
them ("सामने" -> "saamne"), so it applies Hindi's schwa deletion instead of
reading every consonant with an 'a'.
"""

from __future__ import annotations

import re
import unicodedata

_VIRAMA = "्"
_NUKTA = "़"
_ANUSVARA = "ं"
_CHANDRABINDU = "ँ"
_VISARGA = "ः"

_INDEPENDENT_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "oo", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}  # fmt: skip

_MATRAS = {
    "ा": "aa", "ि": "i", "ी": "i", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}  # fmt: skip

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}  # fmt: skip

# consonant + nukta (क़ ख़ ग़ ज़ ड़ ढ़ फ़ य़) -> sound
_NUKTA_FORMS = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f"}

_DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
_LABIALS = set("pbm")


def _is_deva(ch: str) -> bool:
    return "ऀ" <= ch <= "ॿ"


def _syllables(word: str) -> list[list]:
    """Split a Devanagari word into syllable units ``[onset, vowel, inherent, n_consonants]``.
    ``inherent`` marks a consonant that carries only the implied 'a' (the schwa
    candidates for deletion)."""
    units: list[list] = []
    i, n = 0, len(word)
    pending = ""  # consonants joined by virama (a conjunct) waiting for their vowel
    count = 0  # how many consonants are in `pending`
    while i < n:
        ch = word[i]
        if ch in _CONSONANTS:
            sound = _CONSONANTS[ch]
            if i + 1 < n and word[i + 1] == _NUKTA:
                sound = _NUKTA_FORMS.get(ch, sound)
                i += 1
            # ज्ञ is pronounced "gy", not "jn".
            if pending == "j" and ch == "ञ":
                pending, sound, count = "", "gy", 0
            pending += sound
            count += 1
            nxt = word[i + 1] if i + 1 < n else ""
            if nxt == _VIRAMA:
                i += 2
                continue
            if nxt in _MATRAS:
                units.append([pending, _MATRAS[nxt], False, count])
                i += 2
            else:
                units.append([pending, "a", True, count])
                i += 1
            pending, count = "", 0
        elif ch in _INDEPENDENT_VOWELS:
            units.append(["", _INDEPENDENT_VOWELS[ch], False, 0])
            i += 1
        elif ch in (_ANUSVARA, _CHANDRABINDU):
            units.append(["", "\x00nasal", False, 0])  # resolved once the next sound is known
            i += 1
        elif ch == _VISARGA:
            units.append(["h", "", False, 1])
            i += 1
        elif ch in _DIGITS:
            units.append([_DIGITS[ch], "", False, 0])
            i += 1
        else:  # stray virama / nukta / matra without a consonant
            i += 1
    if pending:  # word ends in a virama: "विद्" -> consonants without a vowel
        units.append([pending, "", False, count])
    return units


def _drop_schwas(units: list[list]) -> None:
    """Hindi doesn't say every inherent 'a': the one at the end of a word, and
    the one between two vowel-bearing syllables ("सामने" -> saamne), are silent.
    Worked from the right so each decision sees its right neighbour's final state."""
    last = len(units) - 1
    for i in range(last, -1, -1):
        onset, vowel, inherent, n_cons = units[i]
        if not inherent:
            continue
        if i == last:
            # Sanskrit-style endings keep it: "रहस्य" rahasya, "क्षेत्र" kshetra.
            if not (n_cons > 1 and onset[-1] in "yr"):
                units[i][1] = ""
        elif i > 0 and units[i - 1][1] and units[i + 1][1] and units[i + 1][3] < 2:
            # (not before a conjunct: "रहस्य" must stay rahasya, not "rahsya")
            units[i][1] = ""


def _word_to_roman(word: str) -> str:
    units = _syllables(word)
    if not units:
        return word
    _drop_schwas(units)
    out: list[str] = []
    for idx, (onset, vowel, _, _n) in enumerate(units):
        if vowel == "\x00nasal":
            # Next sound decides: before a lip sound it is "m", otherwise "n".
            nxt = units[idx + 1][0][:1] if idx + 1 < len(units) else ""
            out.append("m" if nxt in _LABIALS else "n")
        else:
            out.append(onset + vowel)
    text = "".join(out)
    if text == "men":
        return "mein"
    # People type a final long vowel short: kya, tha, gaya (not kyaa, thaa, gayaa).
    return text[:-1] if text.endswith("aa") and len(text) > 2 else text


_DEVA_RUN = re.compile(r"[ऀ-ॿ]+")


def contains_devanagari(text: str) -> bool:
    return any(_is_deva(ch) for ch in text)


def has_words(text: str) -> bool:
    """True when ``text`` has spoken words (Whisper writes "♪" for music)."""
    return any(ch.isalpha() for ch in text)


def is_mostly_devanagari(text: str) -> bool:
    """True when ``text`` is Hindi in Devanagari: over 30% of its letters.
    One Hindi word in an English sentence doesn't count."""
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(1 for ch in letters if _is_deva(ch)) / len(letters) > 0.3


def romanize(text: str) -> str:
    """Replace every Devanagari word in ``text`` with its Roman spelling; other
    characters are left alone. Sentence starts are capitalised."""
    if not contains_devanagari(text):
        return text
    text = unicodedata.normalize("NFC", text).replace("॥", ".").replace("।", ".")
    text = _DEVA_RUN.sub(lambda m: _word_to_roman(m.group(0)), text)
    return re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
