from __future__ import annotations

import shutil
import subprocess

import pytest

from app.config.settings import get_settings
from app.services.transcriber import (
    OpenAITranscriber,
    Word,
    _merge_chunks,
    _sentences_from_words,
)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_openai_transcriber_slices_chunks_and_reports_progress(tmp_path, monkeypatch):
    # ~22 min of audio -> chunks at 0s, 595s, 1190s (10-min chunks, 5s overlap).
    audio = tmp_path / "audio.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1300",
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k", str(audio)],
        check=True,
    )  # fmt: skip
    transcriber = OpenAITranscriber(get_settings())
    monkeypatch.setattr(transcriber, "_client", lambda: None)
    monkeypatch.setattr(
        transcriber, "_transcribe_chunk", lambda client, path, lang: [Word(" hi", 1.0, 1.5)]
    )

    seen: list[float] = []
    result = transcriber.transcribe(audio, on_progress=seen.append)

    assert len(seen) == 3 and seen == sorted(seen) and seen[-1] == 1.0
    # Each chunk's word is shifted by its offset in the full audio.
    assert [round(w.start) for w in result.words] == [1, 596, 1191]


def _mk(words):
    return [Word(word=w, start=s, end=e) for w, s, e in words]


def test_sentences_split_on_pause_without_punctuation():
    # OpenAI word tokens have no punctuation; a pause > max_gap must still split.
    words = _mk(
        [
            (" Welcome", 0.0, 0.3),
            (" everyone", 0.3, 0.8),
            # 1.0s pause here -> boundary
            (" Today", 1.8, 2.1),
            (" we", 2.1, 2.3),
            (" begin", 2.3, 2.7),
        ]
    )
    segs = _sentences_from_words(words, max_gap_s=0.6)
    assert len(segs) == 2
    assert segs[0]["text"] == "Welcome everyone"
    assert segs[1]["text"] == "Today we begin"


def test_sentences_split_on_max_duration():
    # Continuous speech with no pause and no punctuation still gets chunked.
    words = _mk([(f"w{i}", i * 0.5, i * 0.5 + 0.5) for i in range(40)])  # 20s total
    segs = _sentences_from_words(words, max_gap_s=5.0, max_dur_s=8.0)
    assert len(segs) >= 2
    for s in segs:
        assert (s["end"] - s["start"]) <= 8.5


def test_sentences_split_on_punctuation():
    words = _mk([(" Hi.", 0.0, 0.4), (" There", 0.5, 0.9), (" friend.", 0.9, 1.3)])
    segs = _sentences_from_words(words, max_gap_s=5.0)
    assert len(segs) == 2
    assert segs[0]["text"] == "Hi."


def test_merge_chunks_dedups_overlap():
    # Chunk 0: words at 0..5s. Chunk 1 offset 4s repeats the 4..5s overlap.
    c0 = _mk([("a", 0.0, 1.0), ("b", 4.0, 4.5)])
    c1 = _mk([("b", 0.0, 0.5), ("c", 2.0, 2.5)])  # offset 4 -> b at 4.0, c at 6.0
    merged = _merge_chunks([(0.0, c0), (4.0, c1)])
    texts = [w.word for w in merged]
    assert texts == ["a", "b", "c"]  # duplicate "b" dropped
    assert merged[-1].start == 6.0
