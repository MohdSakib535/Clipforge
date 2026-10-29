"""Upload to bucket (Create a reel): a finished reel is copied to an
S3-compatible bucket; the app remembers where, and notices when the video
changes after the upload."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config.settings import get_settings
from app.models import Clip, ClipStatus, Job, JobStatus, Reel, Video, VideoStatus
from app.services import bucket

SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"


class FakeS3:
    def __init__(self):
        self.uploads: list[tuple] = []  # (bucket, key, content type, bytes)
        self.deleted: list[str] = []
        self.fail = None  # called instead of uploading

    def upload_file(self, path, bucket_name, key, ExtraArgs):
        if self.fail:
            self.fail()
        self.uploads.append((bucket_name, key, ExtraArgs["ContentType"], Path(path).read_bytes()))

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.example/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"


@pytest.fixture
def s3(monkeypatch):
    # Set every BUCKET_* here: inside Docker the real .env is in the environment.
    for name, value in {"NAME": "my-reels", "PREFIX": "clipforge", "PUBLIC_URL": ""}.items():
        monkeypatch.setenv(f"BUCKET_{name}", value)
    get_settings.cache_clear()
    fake = FakeS3()
    monkeypatch.setattr(bucket, "_client", lambda settings: fake)
    return fake


def _topic_reel(client, db_session, tmp_path) -> Reel:
    body = {"topic": "3 morning habits", "platform": "instagram", "language": "hi"}
    reel = db_session.get(Reel, client.post("/reels", json=body).json()["id"])
    reel.script = {
        "title": "Morning habits",
        "scenes": [{"narration": "Drink water first.", "image_prompt": "a glass of water"}],
    }
    reel.status = "ready"
    reel.file_path = str(tmp_path / "reel.mp4")
    Path(reel.file_path).write_bytes(b"reel video")
    db_session.commit()
    return reel


def _trim_reel(client, db_session, tmp_path, **fields) -> Clip:
    resp = client.post(
        "/videos/upload", files={"file": ("Garden tour.mp4", b"x" * 10, "video/mp4")}
    )
    video = db_session.get(Video, resp.json()["id"])
    video.status = VideoStatus.ready
    video.rights_confirmed_at = datetime.now(UTC)
    video.duration_s = 60.0
    job = Job(video_id=video.id, platforms=["youtube_shorts"], status=JobStatus.completed)
    db_session.add(job)
    db_session.commit()
    clip = Clip(
        job_id=job.id, platform="youtube_shorts", start_s=10.0, end_s=20.0, rank=1,
        file_path=str(tmp_path / "part.mp4"), **{"status": ClipStatus.ready, **fields},
    )  # fmt: skip
    Path(clip.file_path).write_bytes(b"part video")
    db_session.add(clip)
    db_session.commit()
    return clip


def _touch(path: str) -> None:
    """The file was written again (made again, colour, music)."""
    mtime = Path(path).stat().st_mtime_ns + 10**9
    os.utime(path, ns=(mtime, mtime))


def test_reel_is_copied_to_the_bucket(client, db_session, tmp_path, s3):
    reel = _topic_reel(client, db_session, tmp_path)
    assert client.get(f"/reels/{reel.id}").json()["bucket"] is None

    body = client.post(f"/reels/{reel.id}/upload-to-bucket").json()
    key = f"clipforge/reels/Morning_habits-{reel.id[:8]}.mp4"
    assert body["bucket"]["key"] == key and body["bucket"]["current"] is True
    assert s3.uploads == [("my-reels", key, "video/mp4", b"reel video")]
    assert client.get("/reels").json()[0]["bucket"]["key"] == key

    # "Open": a signed link, so the bucket can stay private.
    resp = client.get(f"/reels/{reel.id}/bucket", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == f"https://signed.example/my-reels/{key}?ttl=604800"


def test_a_changed_reel_shows_as_not_uploaded_and_replaces_its_copy(
    client, db_session, tmp_path, s3
):
    reel = _topic_reel(client, db_session, tmp_path)
    client.post(f"/reels/{reel.id}/upload-to-bucket")
    reel.script = {**reel.script, "title": "A new title"}
    Path(reel.file_path).write_bytes(b"new video")
    _touch(reel.file_path)
    db_session.commit()
    assert client.get(f"/reels/{reel.id}").json()["bucket"]["current"] is False

    body = client.post(f"/reels/{reel.id}/upload-to-bucket").json()
    assert body["bucket"]["current"] is True
    key = f"clipforge/reels/Morning_habits-{reel.id[:8]}.mp4"
    assert [(k, data) for _, k, _, data in s3.uploads] == [
        (key, b"reel video"),
        (key, b"new video"),  # the same object: the copy is replaced
    ]


def test_only_a_finished_reel_is_uploaded(client, db_session, tmp_path, s3):
    reel = _topic_reel(client, db_session, tmp_path)
    reel.status = "generating"
    db_session.commit()
    assert client.post(f"/reels/{reel.id}/upload-to-bucket").status_code == 409
    reel.status = "ready"
    reel.file_path = None
    db_session.commit()
    assert client.post(f"/reels/{reel.id}/upload-to-bucket").status_code == 404
    assert client.get(f"/reels/{reel.id}/bucket").status_code == 404
    assert s3.uploads == []


def test_without_a_bucket_the_user_is_told_how_to_set_one_up(
    client, db_session, tmp_path, monkeypatch
):
    monkeypatch.setenv("BUCKET_NAME", "")
    get_settings.cache_clear()
    reel = _topic_reel(client, db_session, tmp_path)
    resp = client.post(f"/reels/{reel.id}/upload-to-bucket")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "bucket_not_set_up"
    assert "BUCKET_NAME" in resp.json()["error"]["message"]


def test_bucket_errors_are_explained(client, db_session, tmp_path, s3):
    boto_errors = pytest.importorskip("botocore.exceptions")
    s3_failed = pytest.importorskip("boto3.exceptions").S3UploadFailedError

    def no_such_bucket():  # how upload_file reports it: wrapped
        try:
            raise boto_errors.ClientError({"Error": {"Code": "NoSuchBucket"}}, "PutObject")
        except boto_errors.ClientError as exc:
            raise s3_failed("Failed to upload") from exc

    def unreachable():
        raise boto_errors.EndpointConnectionError(endpoint_url="http://minio:9000")

    reel = _topic_reel(client, db_session, tmp_path)
    s3.fail = no_such_bucket
    resp = client.post(f"/reels/{reel.id}/upload-to-bucket")
    assert resp.status_code == 502
    assert resp.json()["error"] == {
        "code": "bucket_failed",
        "message": "There is no bucket 'my-reels'. Check BUCKET_NAME (and BUCKET_REGION / "
        "BUCKET_ENDPOINT_URL).",
    }
    s3.fail = unreachable
    message = client.post(f"/reels/{reel.id}/upload-to-bucket").json()["error"]["message"]
    assert message.startswith("Could not reach the bucket (AWS S3).")
    assert client.get(f"/reels/{reel.id}").json()["bucket"] is None  # nothing recorded


def test_trimmed_reel_goes_with_its_captions(client, db_session, tmp_path, s3):
    clip = _trim_reel(client, db_session, tmp_path)
    Path(clip.file_path).with_suffix(".srt").write_text(SRT)

    body = client.post(f"/clips/{clip.id}/upload-to-bucket").json()
    key = f"clipforge/clips/Garden_tour/youtube_shorts/part-001-{clip.id[:8]}.mp4"
    assert s3.uploads == [
        ("my-reels", key.removesuffix(".mp4") + ".srt", "application/x-subrip", SRT.encode()),
        ("my-reels", key, "video/mp4", b"part video"),
    ]
    assert body["bucket"]["key"] == key and body["bucket"]["current"] is True

    _touch(clip.file_path)  # e.g. a new colour
    listed = client.get(f"/jobs/{clip.job_id}/clips").json()
    assert listed[0]["bucket"]["current"] is False


def test_each_dub_gets_its_own_copy(client, db_session, tmp_path, s3):
    dub = {"language": "hi", "voice": "hf_alpha", "lines": [], "source_clip": "x"}
    clip = _trim_reel(client, db_session, tmp_path, extra={"dub": dub})
    client.post(f"/clips/{clip.id}/upload-to-bucket")
    assert s3.uploads[0][1].endswith(f"/part-001-hi-{clip.id[:8]}.mp4")


def test_a_reel_being_remade_is_not_uploaded(client, db_session, tmp_path, s3):
    clip = _trim_reel(client, db_session, tmp_path, status=ClipStatus.rendering)
    resp = client.post(f"/clips/{clip.id}/upload-to-bucket")
    assert resp.status_code == 409 and s3.uploads == []
    assert client.get(f"/clips/{clip.id}/bucket").status_code == 404


def test_public_bucket_links_need_no_signing(client, db_session, tmp_path, s3, monkeypatch):
    monkeypatch.setenv("BUCKET_PUBLIC_URL", "https://cdn.example.com/")
    get_settings.cache_clear()
    clip = _trim_reel(client, db_session, tmp_path)
    key = client.post(f"/clips/{clip.id}/upload-to-bucket").json()["bucket"]["key"]
    resp = client.get(f"/clips/{clip.id}/bucket", follow_redirects=False)
    assert resp.headers["location"] == f"https://cdn.example.com/{key}"


def test_a_copy_deleted_from_the_bucket_is_forgotten(client, db_session, tmp_path, s3):
    reel = _topic_reel(client, db_session, tmp_path)
    clip = _trim_reel(client, db_session, tmp_path, extra={"has_speech": True})
    reel_key = client.post(f"/reels/{reel.id}/upload-to-bucket").json()["bucket"]["key"]
    clip_key = client.post(f"/clips/{clip.id}/upload-to-bucket").json()["bucket"]["key"]

    assert client.delete("/bucket/videos", params={"key": reel_key}).status_code == 204
    assert client.get(f"/reels/{reel.id}").json()["bucket"] is None
    listed = client.get(f"/jobs/{clip.job_id}/clips").json()
    assert listed[0]["bucket"]["key"] == clip_key  # another file: still in the bucket

    assert client.delete("/bucket/videos", params={"key": clip_key}).status_code == 204
    assert client.get(f"/jobs/{clip.job_id}/clips").json()[0]["bucket"] is None
    db_session.expire_all()
    assert db_session.get(Clip, clip.id).extra == {"has_speech": True}  # the rest is kept
    assert s3.deleted == [reel_key, clip_key, clip_key.removesuffix(".mp4") + ".srt"]
    # It can go up again, as the first time.
    assert client.post(f"/reels/{reel.id}/upload-to-bucket").json()["bucket"]["key"] == reel_key
