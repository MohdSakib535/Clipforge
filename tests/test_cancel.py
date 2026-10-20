"""Start over: running work stops and what it made is deleted; the video and
its transcript are kept; finished work is never deleted."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Clip, ClipStatus, Job, JobStatus, Transcript, Video, VideoStatus
from app.services import downloader
from app.services.storage import get_storage
from app.tasks import downloads, pipeline


@pytest.fixture
def factory(db_session, monkeypatch):
    f = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", f)
    monkeypatch.setattr(downloads, "SessionLocal", f)
    monkeypatch.setattr(pipeline.analysis, "detect_scenes", lambda path: [])
    # No real media in these tests: skip extracting the transcription audio.
    monkeypatch.setattr(pipeline, "_ensure_audio", lambda video, donor=None: "/x/audio.mp3")
    return f


def _gone(db_session, model, row_id) -> bool:
    """The row was deleted by another session (get() would try to refresh
    the stale object this session still holds)."""
    return db_session.query(model).filter(model.id == row_id).count() == 0


def _mark_cancelled(factory, job_id):
    """What POST /jobs/{id}/cancel does to a running job (another process)."""
    with factory() as s:
        s.get(Job, job_id).status = JobStatus.cancelled
        s.commit()


def _video_and_job(db_session, tmp_path, *, transcript: bool) -> tuple[Video, Job]:
    video = Video(
        user_id="u1", filename="film.mp4", path=str(tmp_path / "film.mp4"),
        status=VideoStatus.ready, duration_s=300.0, width=1280, height=720, audio_tracks=0,
        ff_metadata={"video_codec": "h264", "pix_fmt": "yuv420p"},
    )  # fmt: skip
    db_session.add(video)
    db_session.commit()
    if transcript:
        segs = [{"text": f"line {i}", "start": i * 10.0, "end": i * 10.0 + 3} for i in range(30)]
        words = [
            {"word": f" w{i}", "start": s["start"], "end": s["end"]} for i, s in enumerate(segs)
        ]
        db_session.add(
            Transcript(video_id=video.id, language="hi", provider="test", duration_s=300,
                       text="", words=words, segments=segs)
        )  # fmt: skip
    job = Job(
        video_id=video.id, platforms=["facebook_video"], status=JobStatus.queued,
        settings={"coverage_lengths": {"facebook_video": 60}, "captions": {"enabled": True}},
    )  # fmt: skip
    db_session.add(job)
    db_session.commit()
    return video, job


def test_cancel_during_transcription_deletes_the_job_keeps_the_video(
    db_session, tmp_path, factory, monkeypatch
):
    video, job = _video_and_job(db_session, tmp_path, transcript=False)
    job_id = job.id

    def transcribe_then_start_over(db, v, cost, on_progress=None):
        on_progress(0.3)
        _mark_cancelled(factory, job_id)  # user clicks Start over
        on_progress(0.6)  # the pipeline notices here
        raise AssertionError("transcription should have stopped")

    monkeypatch.setattr(pipeline, "_transcribe", transcribe_then_start_over)
    pipeline.run_job.apply(args=[job_id])
    db_session.expire_all()
    assert _gone(db_session, Job, job_id)  # gone, not "failed"
    assert db_session.get(Video, video.id) is not None  # the video stays


def test_cancel_during_rendering_stops_and_deletes_every_clip(
    db_session, tmp_path, factory, monkeypatch
):
    from app.config.settings import get_settings

    monkeypatch.setenv("COPY_CONCURRENCY", "1")  # render one at a time: deterministic
    get_settings.cache_clear()
    video, job = _video_and_job(db_session, tmp_path, transcript=True)
    job_id = job.id
    rendered = []

    def fake_render(spec):
        Path(spec.out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(spec.out_path).write_bytes(b"mp4")
        rendered.append(spec.out_path)
        if len(rendered) == 2:
            _mark_cancelled(factory, job_id)  # Start over after 2 clips

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    pipeline.run_job.apply(args=[job_id])
    db_session.expire_all()

    assert 2 <= len(rendered) < 5  # stopped early, not all 5 parts
    assert _gone(db_session, Job, job_id)
    assert db_session.query(Clip).filter(Clip.job_id == job_id).count() == 0
    assert not any(Path(p).parent.exists() for p in rendered)  # clip folders deleted
    assert db_session.get(Video, video.id) is not None
    assert db_session.query(Transcript).filter(Transcript.video_id == video.id).count() == 1


def test_job_cancelled_before_it_started_is_deleted(db_session, tmp_path, factory):
    _, job = _video_and_job(db_session, tmp_path, transcript=True)
    job_id = job.id
    job.status = JobStatus.cancelled
    db_session.commit()
    pipeline.run_job.apply(args=[job_id])
    db_session.expire_all()
    assert _gone(db_session, Job, job_id)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def _ready_video(client, db_session) -> str:
    vid = client.post("/videos/upload", files={"file": ("v.mp4", b"\x00fake", "video/mp4")}).json()[
        "id"
    ]
    v = db_session.get(Video, vid)
    v.status = VideoStatus.ready
    v.rights_confirmed_at = datetime.now(UTC)
    v.duration_s = 600.0
    db_session.commit()
    return vid


def test_cancel_endpoint(client, db_session):
    vid = _ready_video(client, db_session)
    queued = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()
    r = client.post(f"/jobs/{queued['id']}/cancel")
    assert r.json() == {"status": "cancelled", "deleted": True}
    assert client.get(f"/jobs/{queued['id']}").status_code == 404

    running = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()
    db_session.get(Job, running["id"]).status = JobStatus.running
    db_session.commit()
    assert client.post(f"/jobs/{running['id']}/cancel").json()["status"] == "cancelling"
    assert db_session.get(Job, running["id"]).status == JobStatus.cancelled

    done = client.post(f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"]}).json()
    job = db_session.get(Job, done["id"])
    job.status = JobStatus.completed
    db_session.add(
        Clip(job_id=job.id, platform="facebook_video", start_s=0, end_s=60, rank=1,
             status=ClipStatus.ready)
    )  # fmt: skip
    db_session.commit()
    r = client.post(f"/jobs/{done['id']}/cancel")
    assert r.json() == {"status": "completed", "deleted": False}  # finished work is kept
    assert client.get(f"/jobs/{done['id']}/clips").json()[0]["rank"] == 1


def test_abort_unfinished_upload_only(client, db_session):
    init = client.post(
        "/videos/upload/init", json={"filename": "big.mp4", "size_bytes": 100}
    ).json()
    vid = init["video_id"]
    client.put(f"/videos/{vid}/upload/chunk?offset=0", content=b"\x00" * 10)
    folder = get_storage().path_for(f"videos/{vid}")
    assert folder.exists()
    assert client.post(f"/videos/{vid}/abort").status_code == 204
    assert client.get(f"/videos/{vid}").status_code == 404 and not folder.exists()

    ready = _ready_video(client, db_session)
    r = client.post(f"/videos/{ready}/abort")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_in_progress"


# ---------------------------------------------------------------------------
# Link download stopped by Start over
# ---------------------------------------------------------------------------
def test_download_stops_and_cleans_up_when_the_video_is_deleted(db_session, factory, monkeypatch):
    video = Video(
        user_id="u1", filename="x", status=VideoStatus.downloading,
        source_url="https://example.com/v", download_progress=0,
    )  # fmt: skip
    db_session.add(video)
    db_session.commit()
    vid = video.id

    def fake_download(url, dest_dir, quality, on_progress=None, **kw):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "source.f137.mp4.part").write_bytes(b"partial")
        on_progress(10)
        with factory() as s:  # what POST /videos/{id}/abort does
            s.delete(s.get(Video, vid))
            s.commit()
        on_progress(20)  # raises: the row is gone
        raise AssertionError("download should have stopped")

    monkeypatch.setattr(downloader, "download", fake_download)
    downloads.download_video.apply(args=[vid])
    assert not get_storage().path_for(f"videos/{vid}").exists()  # partial file removed
