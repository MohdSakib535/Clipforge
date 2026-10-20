"""Transcription can be switched off per job (Settings step)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.config.settings import Settings
from app.models import Clip, ClipStatus, Job, JobStatus, Video, VideoStatus
from app.schemas.job import JobCreate
from app.tasks import pipeline


def test_off_turns_captions_off_and_default_is_on():
    assert JobCreate(platforms=["facebook_video"]).transcribe is True
    off = JobCreate(platforms=["facebook_video"], transcribe=False, captions={"enabled": True})
    assert off.captions.enabled is False  # captions come from the transcript


def test_api_stores_the_choice(client, db_session):
    vid = client.post("/videos/upload", files={"file": ("v.mp4", b"\x00fake", "video/mp4")}).json()[
        "id"
    ]
    v = db_session.get(Video, vid)
    v.status = VideoStatus.ready
    v.rights_confirmed_at = datetime.now(UTC)
    v.duration_s = 600.0
    db_session.commit()
    job = client.post(
        f"/videos/{vid}/jobs", json={"platforms": ["facebook_video"], "transcribe": False}
    ).json()
    settings = db_session.get(Job, job["id"]).settings
    assert settings["transcribe"] is False and settings["captions"]["enabled"] is False


def test_job_without_transcription_never_transcribes(db_session, tmp_path, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    monkeypatch.setattr(pipeline.analysis, "detect_scenes", lambda path: [59.5, 118.0])

    def must_not_run(*a, **k):
        raise AssertionError("transcription was switched off")

    monkeypatch.setattr(pipeline, "_transcribe", must_not_run)
    monkeypatch.setattr(Settings, "check_transcriber_config", must_not_run)

    def fake_render(spec):
        Path(spec.out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(spec.out_path).write_bytes(b"mp4")

    monkeypatch.setattr(pipeline, "render_clip", fake_render)

    video = Video(
        user_id="u1", filename="film.mp4", path=str(tmp_path / "film.mp4"),
        status=VideoStatus.ready, duration_s=180.0, width=1280, height=720, audio_tracks=0,
        ff_metadata={"video_codec": "h264", "pix_fmt": "yuv420p"},
    )  # fmt: skip
    db_session.add(video)
    db_session.commit()
    job = Job(
        video_id=video.id, platforms=["facebook_video"], status=JobStatus.queued,
        settings={"transcribe": False, "coverage_lengths": {"facebook_video": 60},
                  "captions": {"enabled": False}},
    )  # fmt: skip
    db_session.add(job)
    db_session.commit()
    job_id = job.id

    pipeline.run_job.apply(args=[job_id])
    db_session.expire_all()
    job = db_session.query(Job).filter(Job.id == job_id).one()
    assert job.status == JobStatus.completed, job.error
    clips = db_session.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.rank).all()
    assert [c.title for c in clips] == ["Part 1", "Part 2", "Part 3"]
    assert clips[0].end_s == 59.5  # snapped to the scene change just before 60 s
    assert all(c.status == ClipStatus.ready and not c.captions_enabled for c in clips)
    assert all(c.has_speech is None for c in clips)  # unknown, not "No speech"
