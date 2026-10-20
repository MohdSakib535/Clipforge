"""A whole job, end to end: transcript -> whole-video parts -> rendered clips."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Clip, ClipStatus, Job, JobStage, JobStatus, Transcript, Video, VideoStatus
from app.tasks import pipeline
from tests.synthetic_media import make_media


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_job_cuts_the_whole_video_into_titled_clips(db_session, tmp_path, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)

    src = make_media(tmp_path / "film.mp4", seed=3, seconds=130)
    video = Video(
        user_id="u1", filename="film.mp4", path=str(src), status=VideoStatus.ready,
        duration_s=130.0, width=160, height=90, fps=10.0,
        ff_metadata={"video_codec": "h264", "pix_fmt": "yuv420p"},
    )  # fmt: skip
    db_session.add(video)
    db_session.commit()
    # Already transcribed (so Whisper isn't called): a sentence every 10 s,
    # with nothing said after 100 s.
    words, segments = [], []
    for i in range(10):
        t0 = i * 10.0
        said = [(" namaste", t0, t0 + 1), (" dosto", t0 + 1, t0 + 2), (f" part{i}", t0 + 2, t0 + 3)]
        words += [{"word": w, "start": s, "end": e} for w, s, e in said]
        segments.append({"text": f"namaste dosto part{i}", "start": t0, "end": t0 + 3})
    db_session.add(
        Transcript(video_id=video.id, language="en", provider="test", duration_s=130,
                   text="", words=words, segments=segments)
    )  # fmt: skip
    job = Job(
        video_id=video.id, platforms=["facebook_video"], status=JobStatus.queued,
        settings={"coverage_lengths": {"facebook_video": 60}, "captions": {"enabled": True}},
    )  # fmt: skip
    db_session.add(job)
    db_session.commit()

    pipeline.run_job.apply(args=[job.id])
    db_session.expire_all()

    job = db_session.get(Job, job.id)
    assert job.status == JobStatus.completed, job.error
    assert job.stage == JobStage.done and job.estimated_cost_usd == 0
    clips = db_session.query(Clip).filter(Clip.job_id == job.id).order_by(Clip.rank).all()
    assert [c.rank for c in clips] == [1, 2] and all(c.status == ClipStatus.ready for c in clips)
    assert clips[0].start_s == 0 and clips[0].end_s == clips[1].start_s  # contiguous
    assert abs(clips[-1].end_s - 130) < 0.5  # the whole video
    assert clips[0].title.startswith("Part 1 - namaste dosto part0")
    assert clips[0].description.startswith("namaste dosto part0")
    assert all(c.hashtags == [] and c.has_speech for c in clips)
    # Each file records the grade it was rendered with (none for a new clip).
    assert all(c.color_rendered == {"contrast": 0, "brightness": 0, "saturation": 0} for c in clips)


def test_rerender_records_the_grade_it_used(db_session, tmp_path, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    used = []

    def fake_render(spec):  # no FFmpeg needed: just write the "clip"
        Path(spec.out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(spec.out_path).write_bytes(b"mp4")
        used.append(spec)

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    video = Video(
        user_id="u1", filename="film.mp4", path=str(tmp_path / "film.mp4"),
        status=VideoStatus.ready, duration_s=600.0, width=1280, height=720,
        ff_metadata={"video_codec": "h264", "pix_fmt": "yuv420p"},
    )  # fmt: skip
    db_session.add(video)
    db_session.commit()
    job = Job(video_id=video.id, platforms=["youtube_clip"], status=JobStatus.completed)
    db_session.add(job)
    db_session.commit()
    clip = Clip(
        job_id=job.id, platform="youtube_clip", start_s=0, end_s=60, rank=1,
        status=ClipStatus.approved, color_contrast=20, color_saturation=-10,
        extra={"color_rendered": {"contrast": 0, "brightness": 0, "saturation": 0}},
    )  # fmt: skip
    db_session.add(clip)
    db_session.commit()

    pipeline.render_single_clip.apply(args=[clip.id, True])
    db_session.expire_all()

    clip = db_session.get(Clip, clip.id)
    assert used[0].color_contrast == 20 and used[0].color_saturation == -10
    assert clip.status == ClipStatus.approved  # an approved clip stays approved
    assert clip.color_rendered == {"contrast": 20, "brightness": 0, "saturation": -10}
