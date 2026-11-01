"""Delete all data (Review screen): every video, job, clip and transcript and
their files go; the music library stays; refused while work is running."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.deps import DEV_EMAIL
from app.models import (
    AudioTrack,
    Clip,
    ClipStatus,
    Job,
    JobStatus,
    Transcript,
    User,
    Video,
    VideoStatus,
)
from app.services import storage as storage_mod
from app.services.discard import CUT_OFF, release_stuck_clips
from app.services.storage import LocalStorageService
from app.tasks import dispatch


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A storage root of its own: the shared test storage holds other tests' files."""
    s = LocalStorageService(tmp_path / "storage")
    monkeypatch.setattr(storage_mod, "_storage", s)
    return s


def _write(store, key: str, size: int) -> None:
    p = store.path_for(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * size)


def _data(db_session, store) -> dict:
    user = User(email=DEV_EMAIL)
    db_session.add(user)
    db_session.commit()
    video = Video(user_id=user.id, filename="film.mp4", status=VideoStatus.ready, duration_s=120)
    track = AudioTrack(user_id=user.id, filename="song.m4a", status="ready")
    db_session.add_all([video, track])
    db_session.commit()
    track.path = str(store.path_for(f"audio/{track.id}/track.m4a"))
    job = Job(video_id=video.id, platforms=["facebook_video"], status=JobStatus.completed)
    transcript = Transcript(video_id=video.id, language="hi", provider="test", duration_s=120,
                            text="", words=[], segments=[])  # fmt: skip
    db_session.add_all([job, transcript])
    db_session.commit()
    clips = [
        Clip(job_id=job.id, platform="facebook_video", start_s=i * 60, end_s=i * 60 + 60,
             rank=i + 1, status=ClipStatus.ready)
        for i in range(2)
    ]  # fmt: skip
    db_session.add_all(clips)
    db_session.commit()

    _write(store, f"videos/{video.id}/source.mp4", 1000)
    _write(store, f"videos/{video.id}/transcript_chunks/0.json", 50)
    for c in clips:
        _write(store, f"clips/{c.id}/clip.mp4", 300)
    _write(store, "clips/left-over/clip.mp4", 7)  # no row points to it
    _write(store, f"audio/{track.id}/track.m4a", 40)
    _write(store, ".hf-cache/model.bin", 90)
    return {"video": video, "job": job, "clips": clips, "track": track}


def test_usage_then_delete_everything_but_music(client, db_session, store):
    d = _data(db_session, store)
    usage = client.get("/data/usage").json()
    assert usage == {"videos": 1, "jobs": 1, "clips": 2, "bytes": 1000 + 50 + 2 * 300 + 7}

    r = client.post("/data/delete-all", json={"confirm": "DELETE"})
    assert r.status_code == 200
    assert r.json() == {"videos": 1, "jobs": 1, "clips": 2, "bytes_freed": 1657}

    db_session.expire_all()
    assert db_session.query(Video).count() == 0
    assert db_session.query(Job).count() == 0
    assert db_session.query(Clip).count() == 0
    assert db_session.query(Transcript).count() == 0
    assert not any(store.path_for("videos").iterdir())
    assert not any(store.path_for("clips").iterdir())  # left-over folder swept too
    # Kept: the music library and the Whisper model.
    assert db_session.query(AudioTrack).count() == 1
    assert store.path_for(f"audio/{d['track'].id}/track.m4a").exists()
    assert store.path_for(".hf-cache/model.bin").exists()
    assert client.get("/data/usage").json() == {"videos": 0, "jobs": 0, "clips": 0, "bytes": 0}


def test_needs_typed_confirm(client, db_session, store):
    _data(db_session, store)
    r = client.post("/data/delete-all", json={"confirm": "yes"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_confirmed"
    assert client.post("/data/delete-all", json={}).status_code == 422
    assert db_session.query(Video).count() == 1


@pytest.mark.parametrize("busy", ["job_running", "clip_rendering", "video_downloading"])
def test_refused_while_work_is_running(client, db_session, store, busy):
    d = _data(db_session, store)
    if busy == "job_running":
        d["job"].status = JobStatus.running
    elif busy == "clip_rendering":
        d["clips"][0].status = ClipStatus.rendering
    else:
        d["video"].status = VideoStatus.downloading
    db_session.commit()
    r = client.post("/data/delete-all", json={"confirm": "DELETE"})
    assert r.status_code == 409
    assert db_session.query(Clip).count() == 2  # nothing deleted
    assert store.path_for(f"videos/{d['video'].id}/source.mp4").exists()


def _rendering(db_session, clip, minutes_ago: int) -> None:
    clip.status = ClipStatus.rendering
    clip.updated_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    db_session.commit()


def test_cut_off_rerender_does_not_block_delete_all(client, db_session, store, monkeypatch):
    """A clip left "rendering" by a worker restart (nothing running or queued
    for it) no longer holds up deleting everything."""
    d = _data(db_session, store)
    _rendering(db_session, d["clips"][0], minutes_ago=10)
    monkeypatch.setattr(dispatch, "clip_work_pending", lambda: False)
    r = client.post("/data/delete-all", json={"confirm": "DELETE"})
    assert r.status_code == 200
    assert db_session.query(Clip).count() == 0


def test_cut_off_rerender_does_not_block_deleting_a_video(client, db_session, store, monkeypatch):
    d = _data(db_session, store)
    _rendering(db_session, d["clips"][0], minutes_ago=10)
    monkeypatch.setattr(dispatch, "clip_work_pending", lambda: False)
    assert client.delete(f"/data/videos/{d['video'].id}/clips").status_code == 200


def test_rerender_still_waiting_blocks_and_names_the_video(client, db_session, store, monkeypatch):
    d = _data(db_session, store)
    for c in d["clips"]:
        _rendering(db_session, c, minutes_ago=10)
    monkeypatch.setattr(dispatch, "clip_work_pending", lambda: True)
    r = client.post("/data/delete-all", json={"confirm": "DELETE"})
    assert r.status_code == 409
    assert '2 of "film.mp4"' in r.json()["error"]["message"]
    db_session.expire_all()
    assert {c.status for c in db_session.query(Clip)} == {ClipStatus.rendering}


def test_just_queued_rerender_is_not_taken_for_cut_off(client, db_session, store, monkeypatch):
    d = _data(db_session, store)
    _rendering(db_session, d["clips"][0], minutes_ago=0)  # its task may not be queued yet
    monkeypatch.setattr(dispatch, "clip_work_pending", lambda: False)
    assert client.post("/data/delete-all", json={"confirm": "DELETE"}).status_code == 409
    db_session.refresh(d["clips"][0])
    assert d["clips"][0].status == ClipStatus.rendering


def test_release_marks_cut_off_clips_failed(db_session, store, monkeypatch):
    d = _data(db_session, store)
    monkeypatch.setattr(dispatch, "clip_work_pending", lambda: False)
    jobs = select(Job.id).where(Job.video_id == d["video"].id)
    _rendering(db_session, d["clips"][0], minutes_ago=10)
    assert release_stuck_clips(db_session, jobs) == 1
    db_session.refresh(d["clips"][0])
    assert d["clips"][0].status == ClipStatus.failed
    assert d["clips"][0].error == CUT_OFF
    # A clip of a job still processing is its job's to finish.
    d["job"].status = JobStatus.running
    _rendering(db_session, d["clips"][1], minutes_ago=10)
    assert release_stuck_clips(db_session, jobs) == 0
    db_session.refresh(d["clips"][1])
    assert d["clips"][1].status == ClipStatus.rendering
