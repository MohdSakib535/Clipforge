"""The same film uploaded again must reuse its transcript, never pay twice."""

from __future__ import annotations

import os

import pytest

from app.models import Transcript, Video, VideoStatus
from app.services import filehash
from app.services.cost import CostTracker
from app.services.storage import get_storage
from app.tasks import pipeline


def _video(db, tmp_path, name: str, content: bytes, *, user="u1", fingerprint=True) -> Video:
    path = tmp_path / name
    path.write_bytes(content)
    v = Video(user_id=user, filename=name, path=str(path), status=VideoStatus.ready, duration_s=60)
    if fingerprint:
        v.content_hash = filehash.file_fingerprint(path)
    db.add(v)
    db.commit()
    return v


def _transcript(db, video: Video) -> Transcript:
    t = Transcript(
        video_id=video.id, language="hi", provider="openai:whisper-1", duration_s=60,
        text="namaste", words=[{"word": " namaste", "start": 1.0, "end": 1.5}],
        segments=[{"text": "namaste", "start": 1.0, "end": 1.5}],
    )  # fmt: skip
    db.add(t)
    db.commit()
    # The earlier upload's audio, so reuse needs no ffmpeg.
    audio = get_storage().path_for(f"videos/{video.id}/audio.mp3")
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"mp3")
    return t


@pytest.fixture
def no_transcription(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not transcribe (would be paid for)")

    monkeypatch.setattr(pipeline, "get_transcriber", _fail)


def test_reupload_reuses_transcript_for_free(db_session, tmp_path, no_transcription):
    film = os.urandom(20_000_000)  # bigger than the three samples together
    first = _video(db_session, tmp_path, "film.mp4", film)
    _transcript(db_session, first)
    again = _video(db_session, tmp_path, "film (copy).mp4", film)

    cost = CostTracker(cap_usd=1.0)
    row = pipeline._transcribe(db_session, again, cost)

    assert row.video_id == again.id and row.words == [
        {"word": " namaste", "start": 1.0, "end": 1.5}
    ]
    assert cost.total_usd == 0 and cost.audio_minutes == 0
    assert get_storage().path_for(f"videos/{again.id}/audio.mp3").read_bytes() == b"mp3"


def test_older_upload_without_fingerprint_is_still_matched(db_session, tmp_path, no_transcription):
    film = os.urandom(1_000_000)
    old = _video(db_session, tmp_path, "old.mp4", film, fingerprint=False)  # pre-migration
    _transcript(db_session, old)
    new = _video(db_session, tmp_path, "new.mp4", film)

    pipeline._transcribe(db_session, new, CostTracker(cap_usd=1.0))
    assert old.content_hash == new.content_hash  # worked out on demand, then stored


def test_different_file_or_other_user_is_not_reused(db_session, tmp_path):
    a = _video(db_session, tmp_path, "a.mp4", b"A" * 5000)
    _transcript(db_session, a)
    b = _video(db_session, tmp_path, "b.mp4", b"B" * 5000)  # same size, other content
    assert pipeline._same_file_transcript(db_session, b) is None

    other_user = _video(db_session, tmp_path, "a2.mp4", b"A" * 5000, user="u2")
    assert pipeline._same_file_transcript(db_session, other_user) is None


def test_fingerprint_samples_start_middle_and_end(tmp_path):
    base = bytearray(os.urandom(30_000_000))
    p1, p2, p3 = tmp_path / "1", tmp_path / "2", tmp_path / "3"
    p1.write_bytes(base)
    changed_middle = bytearray(base)
    changed_middle[15_000_000] ^= 0xFF
    p2.write_bytes(changed_middle)
    p3.write_bytes(base)
    assert filehash.file_fingerprint(p1) == filehash.file_fingerprint(p3)
    assert filehash.file_fingerprint(p1) != filehash.file_fingerprint(p2)
    assert filehash.file_fingerprint(p1).startswith("30000000-")
