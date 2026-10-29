"""Local videos: what is kept on this computer (with the disk space it uses),
and deleting one video, only its clips, or one clip, files and all."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.models import (
    AudioTrack,
    Clip,
    ClipStatus,
    Job,
    JobStatus,
    Reel,
    Transcript,
    Video,
    VideoStatus,
)
from app.services.storage import get_storage
from app.tasks import pipeline


def _file(key: str, size: int) -> None:
    path = get_storage().path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _video(client, db_session, name="film.mp4", link=None, clips=2) -> Video:
    """A ready video (1,000 bytes on disk) with ``clips`` clips of 300 bytes."""
    vid = client.post("/videos/upload", files={"file": (name, b"x" * 1000, "video/mp4")}).json()[
        "id"
    ]
    video = db_session.get(Video, vid)
    video.status, video.duration_s, video.source_url = VideoStatus.ready, 90.0, link
    job = Job(video_id=video.id, platforms=["youtube_shorts"], status=JobStatus.completed)
    db_session.add(job)
    db_session.add(Transcript(video_id=video.id, text="hi", words=[], segments=[]))
    db_session.commit()
    for n in range(clips):
        clip = Clip(job_id=job.id, platform="youtube_shorts", start_s=n * 10.0, end_s=n * 10.0 + 10,
                    rank=n + 1, status=ClipStatus.ready)  # fmt: skip
        db_session.add(clip)
        db_session.commit()
        clip.file_path = str(get_storage().path_for(f"clips/{clip.id}/clip.mp4"))
        _file(f"clips/{clip.id}/clip.mp4", 300)
    db_session.commit()
    return video


def _exists(key: str) -> bool:
    return get_storage().path_for(key).exists()


def test_the_list_shows_everything_kept_with_its_size(client, db_session):
    old = _video(client, db_session, "old.mp4")
    linked = _video(client, db_session, "1 MINUTE.mkv", link="https://youtu.be/x", clips=1)
    db_session.query(Clip).filter(
        Clip.job_id.in_([j.id for j in old.jobs])
    ).first().status = ClipStatus.deleted  # hidden from the review screen: not counted as a clip
    user_id = old.user_id
    reel = Reel(user_id=user_id, platform="instagram", language="hi", voice="hf_alpha",
                topic="habits", duration_s=30, status="ready",
                script={"title": "Habits", "scenes": []})  # fmt: skip
    track = AudioTrack(user_id=user_id, filename="song.mp3", status="ready")
    db_session.add_all([reel, track])
    db_session.commit()
    _file(f"reels/{reel.id}/reel.mp4", 500)
    _file(f"audio/{track.id}/song.mp3", 200)

    body = client.get("/data/local").json()
    videos = {v["filename"]: v for v in body["videos"]}
    assert (videos["old.mp4"]["video_bytes"], videos["old.mp4"]["clips_bytes"]) == (1000, 600)
    assert videos["old.mp4"]["clips"] == 1 and videos["old.mp4"]["from_link"] is False
    one = videos["1 MINUTE.mkv"]
    assert (one["from_link"], one["source_url"], one["clips"], one["clips_bytes"]) == (
        True, "https://youtu.be/x", 1, 300,
    )  # fmt: skip
    assert one["transcribed"] is True and one["busy"] is None
    assert [(r["title"], r["bytes"]) for r in body["reels"]] == [("Habits", 500)]
    assert [(t["filename"], t["bytes"]) for t in body["music"]] == [("song.mp3", 200)]
    assert set(body["models"]) == {"whisper", "voice"}
    assert linked.id in {v["id"] for v in body["videos"]}


def test_delete_a_video_with_everything_made_from_it(client, db_session):
    video = _video(client, db_session)
    keep = _video(client, db_session, "keep.mp4")
    vid, keep_id = video.id, keep.id
    clip_ids = [
        c.id for c in db_session.query(Clip).filter(Clip.job_id.in_([j.id for j in video.jobs]))
    ]

    resp = client.delete(f"/data/videos/{vid}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"clips": 2, "bytes_freed": 1600}
    db_session.expire_all()
    assert db_session.query(Video).filter(Video.id == vid).count() == 0
    assert db_session.query(Job).filter(Job.video_id == vid).count() == 0
    assert db_session.query(Clip).filter(Clip.id.in_(clip_ids)).count() == 0
    assert db_session.query(Transcript).filter(Transcript.video_id == vid).count() == 0
    assert not _exists(f"videos/{vid}") and not any(_exists(f"clips/{c}") for c in clip_ids)
    # The other video is untouched.
    assert db_session.get(Video, keep_id) is not None and _exists(f"videos/{keep_id}")
    assert client.delete(f"/data/videos/{vid}").status_code == 404


def test_delete_only_the_clips_keeps_the_video(client, db_session):
    video = _video(client, db_session)
    resp = client.delete(f"/data/videos/{video.id}/clips")
    assert resp.json() == {"clips": 2, "bytes_freed": 600}
    db_session.expire_all()
    assert db_session.query(Job).filter(Job.video_id == video.id).count() == 0
    assert db_session.get(Video, video.id) is not None and _exists(f"videos/{video.id}")
    assert db_session.query(Transcript).filter(Transcript.video_id == video.id).count() == 1
    (listed,) = client.get("/data/local").json()["videos"]
    assert (listed["clips"], listed["clips_bytes"]) == (0, 0)


def test_a_video_being_worked_on_is_not_deleted(client, db_session):
    video = _video(client, db_session)
    job = video.jobs[0]
    for status in (JobStatus.queued, JobStatus.running):
        job.status = status
        db_session.commit()
        resp = client.delete(f"/data/videos/{video.id}")
        assert resp.status_code == 409 and "still being made" in resp.json()["error"]["message"]
        assert client.get("/data/local").json()["videos"][0]["busy"]
    job.status = JobStatus.completed
    clip = job.clips[0]
    clip.status = ClipStatus.rendering
    db_session.commit()
    assert client.delete(f"/data/videos/{video.id}/clips").status_code == 409
    clip.status = ClipStatus.ready
    video.status = VideoStatus.ingesting
    db_session.commit()
    assert client.delete(f"/data/videos/{video.id}").status_code == 409
    # An unfinished download can be: it stops, as on Start over.
    video.status = VideoStatus.downloading
    db_session.commit()
    assert client.delete(f"/data/videos/{video.id}").status_code == 200


def test_deleting_a_clip_frees_its_file(client, db_session):
    video = _video(client, db_session, clips=1)
    clip = video.jobs[0].clips[0]
    clip.status = ClipStatus.rendering
    db_session.commit()
    resp = client.patch(f"/clips/{clip.id}", json={"status": "deleted"})
    assert resp.status_code == 409 and _exists(f"clips/{clip.id}")

    clip.status = ClipStatus.ready
    db_session.commit()
    body = client.patch(f"/clips/{clip.id}", json={"status": "deleted"}).json()
    assert body["status"] == "deleted" and body["has_file"] is False
    assert not _exists(f"clips/{clip.id}")
    assert client.get(f"/jobs/{clip.job_id}/clips").json() == []


def test_a_queued_rerender_does_not_bring_a_deleted_clip_back(db_session, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", factory)

    def no_render(spec):
        raise AssertionError("a deleted clip must not be rendered")

    monkeypatch.setattr(pipeline, "render_clip", no_render)
    video = Video(user_id="u1", filename="f.mp4", status=VideoStatus.ready, duration_s=60)
    db_session.add(video)
    db_session.commit()
    job = Job(video_id=video.id, platforms=["youtube_shorts"], status=JobStatus.completed)
    db_session.add(job)
    db_session.commit()
    clip = Clip(job_id=job.id, platform="youtube_shorts", start_s=0, end_s=10, rank=1,
                status=ClipStatus.deleted)  # fmt: skip
    db_session.add(clip)
    db_session.commit()

    pipeline.render_single_clip.apply(args=[clip.id])

    db_session.expire_all()
    assert db_session.get(Clip, clip.id).status == ClipStatus.deleted


def test_the_original_video_can_be_downloaded(client, db_session):
    video = _video(client, db_session, "My trip.mp4", clips=0)
    resp = client.get(f"/videos/{video.id}/source", params={"download": 1})
    assert resp.status_code == 200 and len(resp.content) == 1000
    # Saved under its own name (the UTF-8 form, as it has a space).
    assert resp.headers["content-disposition"] == "attachment; filename*=utf-8''My%20trip.mp4"
    inline = client.get(f"/videos/{video.id}/source")  # the trim player: plays, isn't saved
    assert "attachment" not in inline.headers.get("content-disposition", "")
    assert Path(video.path).exists()
