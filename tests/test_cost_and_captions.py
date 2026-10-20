from __future__ import annotations

import pytest

from app.services.captions import build_srt
from app.services.cost import CostCapExceeded, CostTracker, transcription_cost


def test_transcription_cost_known_model():
    assert transcription_cost("whisper-1", 10) == pytest.approx(0.06)


def test_cost_tracker_enforces_cap():
    tracker = CostTracker(cap_usd=0.01)
    with pytest.raises(CostCapExceeded):
        tracker.add_transcription("whisper-1", 100)  # 100 min * 0.006 = $0.60 > cap


def test_cost_tracker_accumulates():
    tracker = CostTracker(cap_usd=100.0)
    tracker.add_transcription("whisper-1", 10)
    tracker.add_transcription("large-v3-turbo", 30, free=True)  # local: counted, $0
    assert tracker.audio_minutes == 40
    assert tracker.total_usd == pytest.approx(0.06)
    assert "whisper-1" in tracker.models_used


def _words():
    return [
        {"word": "Hello", "start": 0.0, "end": 0.4},
        {"word": "world", "start": 0.4, "end": 0.9},
        {"word": "this", "start": 1.0, "end": 1.3},
        {"word": "is", "start": 1.3, "end": 1.5},
        {"word": "ClipForge.", "start": 1.5, "end": 2.2},
    ]


def test_build_srt_format():
    srt = build_srt(_words())
    first = srt.split("\n\n")[0].splitlines()
    assert first[0] == "1"
    assert first[1] == "00:00:00,000 --> 00:00:02,200"
    assert first[2] == "Hello world this is ClipForge."


def test_build_srt_long_timestamps():
    words = [{"word": "Late.", "start": 3725.5, "end": 3726.25}]
    assert "01:02:05,500 --> 01:02:06,250" in build_srt(words)
