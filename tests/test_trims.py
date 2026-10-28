"""Reel from a video: parts picked by hand (trims), each made for every chosen
platform, with only the trims' own seconds transcribed."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.models import Clip, ClipStatus, Job, JobStatus, Transcript, Video, VideoStatus
from app.services.transcriber import TranscriptResult, Word
from app.tasks import pipeline
from tests.synthetic_media import make_media


def _ready_video(client, db_session, duration=120.0) -> str:
    resp = client.post("/videos/upload", files={"file": ("film.mp4", b"x" * 10, "video/mp4")})
    vid = resp.json()["id"]
    video = db_session.get(Video, vid)
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = duration
    db_session.commit()
    return vid


def _trim_job(client, vid, trims, platforms=("youtube_shorts", "facebook_reel"), transcribe=True):
    return client.post(
        f"/videos/{vid}/jobs",
        json={"platforms": list(platforms), "trims": trims, "transcribe": transcribe},
    )


# --- API ---------------------------------------------------------------------
def test_trim_job_keeps_each_trim_with_its_platforms(client, db_session):
    vid = _ready_video(client, db_session)
    resp = _trim_job(client, vid, [{"start_s": 0, "end_s": 30}, {"start_s": 40, "end_s": 70}])
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["settings"]["trims"] == [
        {
            "start_s": 0,
            "end_s": 30,
            "platforms": ["youtube_shorts", "facebook_reel"],
            "transcribe": True,
        },
        {
            "start_s": 40,
            "end_s": 70,
            "platforms": ["youtube_shorts", "facebook_reel"],
            "transcribe": True,
        },
    ]
    assert client.enqueued["job"] == [job["id"]]


def test_trim_must_fit_every_chosen_platform(client, db_session):
    vid = _ready_video(client, db_session)
    resp = _trim_job(client, vid, [{"start_s": 0, "end_s": 75}])  # Shorts allow 60 s
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_length"
    assert "YouTube Shorts allows 5-60 s" in resp.json()["error"]["message"]
    assert _trim_job(client, vid, [{"start_s": 0, "end_s": 3}]).status_code == 400  # under 5 s

    resp = _trim_job(client, vid, [{"start_s": 0, "end_s": 75}], platforms=["facebook_reel"])
    assert resp.status_code == 201, resp.text


def test_trim_must_be_inside_the_video(client, db_session):
    vid = _ready_video(client, db_session, duration=120.0)
    resp = _trim_job(client, vid, [{"start_s": 100, "end_s": 130}])
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "out_of_range"
    # The player can overshoot the end a little: kept to the video's length.
    resp = _trim_job(client, vid, [{"start_s": 100, "end_s": 120.3}])
    assert resp.status_code == 201, resp.text
    assert resp.json()["settings"]["trims"][0]["end_s"] == 120.0
    # End before start.
    assert _trim_job(client, vid, [{"start_s": 50, "end_s": 40}]).status_code == 422


def test_more_trims_join_the_same_job(client, db_session):
    vid = _ready_video(client, db_session)
    job_id = _trim_job(client, vid, [{"start_s": 0, "end_s": 30}], platforms=["youtube_shorts"])
    job_id = job_id.json()["id"]

    more = {"trims": [{"start_s": 50, "end_s": 80}], "platforms": ["facebook_reel"]}
    resp = client.post(f"/jobs/{job_id}/trims", json=more)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "job_busy"  # still queued

    job = db_session.get(Job, job_id)
    job.status = JobStatus.completed
    db_session.commit()
    resp = client.post(f"/jobs/{job_id}/trims", json={**more, "transcribe": False})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "queued" and out["progress"] == 0
    assert out["platforms"] == ["youtube_shorts", "facebook_reel"]
    assert [t["start_s"] for t in out["settings"]["trims"]] == [0, 50]
    assert out["settings"]["trims"][1]["platforms"] == ["facebook_reel"]
    assert out["settings"]["trims"][1]["transcribe"] is False
    assert client.enqueued["job"] == [job_id, job_id]


def test_trims_cannot_be_added_to_a_whole_video_job(client, db_session):
    vid = _ready_video(client, db_session)
    job_id = client.post(f"/videos/{vid}/jobs", json={"platforms": ["youtube_shorts"]}).json()["id"]
    job = db_session.get(Job, job_id)
    job.status = JobStatus.completed
    db_session.commit()
    resp = client.post(
        f"/jobs/{job_id}/trims",
        json={"trims": [{"start_s": 0, "end_s": 30}], "platforms": ["youtube_shorts"]},
    )
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_a_trim_job"


def test_source_video_is_served_for_the_trim_player(client, db_session, tmp_path):
    vid = _ready_video(client, db_session)
    video = db_session.get(Video, vid)
    src = tmp_path / "source.mp4"
    src.write_bytes(b"0123456789")
    video.path = str(src)
    db_session.commit()

    resp = client.get(f"/videos/{vid}/source")
    assert resp.status_code == 200 and resp.content == b"0123456789"
    assert resp.headers["content-type"] == "video/mp4"
    part = client.get(f"/videos/{vid}/source", headers={"Range": "bytes=2-4"})
    assert part.status_code == 206 and part.content == b"234"  # seeking

    video.status = VideoStatus.downloading
    db_session.commit()
    assert client.get(f"/videos/{vid}/source").status_code == 409


# --- Pipeline ----------------------------------------------------------------
class _FakeTranscriber:
    """Says "before" in the first second of every piece of audio it gets, then
    "hello" and "world"."""

    def __init__(self):
        self.durations: list[float] = []

    def transcribe(self, audio_path, language=None, on_progress=None, cache_dir=None):
        from app.services import ffmpeg

        duration = ffmpeg.ffprobe_metadata(str(audio_path)).duration_s
        self.durations.append(duration)
        words = [Word(" before", 0.5, 1.0), Word(" hello", 2.0, 2.5), Word(" world", 3.0, 3.5)]
        return TranscriptResult("hello world", words, "en", duration, "fake")


@pytest.fixture
def worker(db_session, monkeypatch):
    """run_job/render_single_clip in-process, with rendering faked."""
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)
    rendered = []

    def fake_render(spec):
        Path(spec.out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(spec.out_path).write_bytes(b"mp4")
        rendered.append(spec)

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    return rendered


def _video(db_session, path, duration=60.0) -> Video:
    video = Video(
        user_id="u1", filename="film.mp4", path=str(path), status=VideoStatus.ready,
        duration_s=duration, width=160, height=90, fps=10.0, audio_tracks=1,
        ff_metadata={"video_codec": "h264", "pix_fmt": "yuv420p"},
    )  # fmt: skip
    db_session.add(video)
    db_session.commit()
    return video


def _job(db_session, video, trims) -> Job:
    job = Job(
        video_id=video.id, platforms=["youtube_shorts", "facebook_reel"], status=JobStatus.queued,
        settings={"trims": trims, "transcribe": True, "captions": {"enabled": True}},
    )  # fmt: skip
    db_session.add(job)
    db_session.commit()
    return job


def _clips(db_session, job) -> list[Clip]:
    db_session.expire_all()
    return db_session.query(Clip).filter(Clip.job_id == job.id).order_by(Clip.rank).all()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_only_the_trimmed_seconds_are_transcribed(db_session, tmp_path, monkeypatch, worker):
    fake = _FakeTranscriber()
    monkeypatch.setattr(pipeline, "get_transcriber", lambda settings=None: fake)
    video = _video(db_session, make_media(tmp_path / "film.mp4", seed=5, seconds=60))
    trim = {"start_s": 20.0, "end_s": 32.0, "transcribe": True}
    job = _job(db_session, video, [{**trim, "platforms": ["youtube_shorts", "facebook_reel"]}])

    pipeline.run_job.apply(args=[job.id])

    db_session.expire_all()
    job = db_session.get(Job, job.id)
    assert job.status == JobStatus.completed, job.error
    # Once, the 12 s of the trim and 1.5 s on each side.
    assert len(fake.durations) == 1 and abs(fake.durations[0] - 15) < 0.5
    assert abs(job.audio_minutes - 0.25) < 0.01
    clips = _clips(db_session, job)
    assert sorted(c.platform for c in clips) == ["facebook_reel", "youtube_shorts"]
    for c in clips:
        assert (c.start_s, c.end_s, c.rank, c.status) == (20.0, 32.0, 1, ClipStatus.ready)
        assert c.title == "Reel 1 - hello world" and c.transcript == "hello world"
        assert c.has_speech and c.captions_enabled
        # Word times are seconds in the video, as captions expect; "before"
        # (19.0-19.5 s) was only heard for context.
        assert c.extra["words"] == [
            {"word": " hello", "start": 20.5, "end": 21.0},
            {"word": " world", "start": 21.5, "end": 22.0},
        ]
    assert all(s.words == clips[0].extra["words"] for s in worker)


def test_a_transcribed_video_is_not_transcribed_again(db_session, tmp_path, monkeypatch, worker):
    def no_whisper(settings=None):
        raise AssertionError("the transcript already exists")

    monkeypatch.setattr(pipeline, "get_transcriber", no_whisper)
    video = _video(db_session, tmp_path / "film.mp4")
    said = [(" one", 5, 6), (" two", 12, 13), (" three", 25, 26)]
    db_session.add(
        Transcript(
            video_id=video.id, text="one two three", segments=[],
            words=[{"word": w, "start": s, "end": e} for w, s, e in said],
        )
    )  # fmt: skip
    trim = {"start_s": 10.0, "end_s": 30.0, "platforms": ["facebook_reel"], "transcribe": True}
    job = _job(db_session, video, [trim])

    pipeline.run_job.apply(args=[job.id])

    (clip,) = _clips(db_session, job)
    assert db_session.get(Job, job.id).status == JobStatus.completed
    assert clip.transcript == "two three" and clip.title == "Reel 1 - two three"


def test_added_trims_make_only_the_new_reels(db_session, tmp_path, worker):
    video = _video(db_session, tmp_path / "film.mp4")
    first = {"start_s": 0.0, "end_s": 10.0, "platforms": ["youtube_shorts"], "transcribe": False}
    job = _job(db_session, video, [first])
    pipeline.run_job.apply(args=[job.id])
    (old,) = _clips(db_session, job)

    # What POST /jobs/{id}/trims does.
    second = {"start_s": 30.0, "end_s": 45.0, "platforms": ["facebook_reel"], "transcribe": False}
    job = db_session.get(Job, job.id)
    job.settings = {**job.settings, "trims": [first, second]}
    job.status = JobStatus.queued
    db_session.commit()
    pipeline.run_job.apply(args=[job.id])

    clips = _clips(db_session, job)
    assert db_session.get(Job, job.id).status == JobStatus.completed
    assert [(c.rank, c.platform, c.start_s) for c in clips] == [
        (1, "youtube_shorts", 0.0),
        (2, "facebook_reel", 30.0),
    ]
    assert clips[0].id == old.id and len(worker) == 2  # the first reel wasn't redone
    # No transcript asked: no captions, and nothing claimed about speech.
    assert clips[1].title == "Reel 2" and clips[1].transcript is None
    assert clips[1].has_speech is None and not clips[1].captions_enabled


def test_rerender_keeps_the_trims_own_words(db_session, tmp_path, worker):
    video = _video(db_session, tmp_path / "film.mp4")
    job = _job(db_session, video, [])
    words = [{"word": " hi", "start": 3.0, "end": 3.4}]
    said = Clip(
        job_id=job.id, platform="youtube_shorts", start_s=2, end_s=12, rank=1,
        status=ClipStatus.ready, extra={"trim": 0, "words": words, "transcript": "hi"},
    )  # fmt: skip
    silent = Clip(
        job_id=job.id, platform="youtube_shorts", start_s=20, end_s=30, rank=2,
        status=ClipStatus.ready, extra={"trim": 1, "has_speech": None},
    )  # fmt: skip
    db_session.add_all([said, silent])
    db_session.commit()

    pipeline.render_single_clip.apply(args=[said.id])
    pipeline.render_single_clip.apply(args=[silent.id])

    db_session.expire_all()
    assert worker[0].words == words and worker[1].words == []
    assert db_session.get(Clip, said.id).has_speech is True
    # Never transcribed: a colour change mustn't make it look silent.
    assert db_session.get(Clip, silent.id).has_speech is None
