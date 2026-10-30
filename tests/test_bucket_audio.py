"""Music in the bucket: a file, or the audio of a link, goes straight to its
own folder (<prefix>/audio/), nothing kept on this computer, never replacing
another file; every audio file in the bucket is listed and can be opened,
downloaded, renamed and deleted, or added to the music library to be picked
as background music (it stays in the bucket)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.orm import sessionmaker

from app.config.settings import get_settings
from app.models import AudioTrack, Reel
from app.services import bgaudio, bucket, downloader
from app.services.ffmpeg import MediaInfo
from app.services.storage import get_storage
from app.tasks import downloads
from tests.test_bucket_uploads import s3  # noqa: F401 - the fake bucket fixture

pytest.importorskip("botocore.exceptions")


def _fake_prepare(src, dst):
    Path(dst).write_bytes(b"converted " + Path(src).read_bytes())
    return MediaInfo(duration_s=12.5, width=0, height=0, fps=0, audio_tracks=1, raw={})


def _send_audio(client, filename: str, data: bytes):
    """What the page does for a music file: start (folder "audio"), one PUT, complete."""
    start = client.post(
        "/bucket/uploads", json={"filename": filename, "size_bytes": len(data), "folder": "audio"}
    )
    if start.status_code != 200:
        return start
    ref = {k: start.json()[k] for k in ("key", "upload_id")}
    part = client.put("/bucket/uploads/part", params={**ref, "number": 1}, content=data).json()
    return client.post("/bucket/uploads/complete", json={**ref, "parts": [part]})


def _bring(client, key: str) -> dict:
    """The bucket file into the music library (as a picker does)."""
    resp = client.post("/audio/from-bucket", json={"key": key})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _copy(client, track_id: str) -> dict | None:
    return next(t for t in client.get("/audio").json() if t["id"] == track_id)["bucket"]


def test_a_music_file_goes_straight_to_the_bucket(client, s3):  # noqa: F811
    resp = _send_audio(client, "My song.mp3", b"mp3")
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == "clipforge/audio/My_song.mp3"
    assert s3.objects["clipforge/audio/My_song.mp3"] == (b"mp3", "audio/mpeg")  # as it was
    assert client.get("/audio").json() == []  # nothing kept on this computer
    # A name in use is never replaced.
    assert _send_audio(client, "My song.mp3", b"other").json()["key"] == (
        "clipforge/audio/My_song-2.mp3"
    )
    # Music to the audio folder, videos to the uploads folder: nothing else.
    assert _send_audio(client, "trip.mp4", b"v").json()["error"]["code"] == "unsupported_type"
    resp = client.post("/bucket/uploads", json={"filename": "song.mp3", "size_bytes": 3})
    assert resp.json()["error"]["code"] == "unsupported_type"


def test_a_link_goes_straight_to_the_bucket(client, db_session, s3, monkeypatch):  # noqa: F811
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(downloads, "SessionLocal", factory)
    monkeypatch.setattr(bgaudio, "prepare_track", _fake_prepare)

    def fake_download(url, dest_dir, on_progress=None, **kwargs):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "source.webm").write_bytes(b"webm")
        return downloader.DownloadResult(path=dest_dir / "source.webm", title="Lo-fi beats / study")

    monkeypatch.setattr(downloader, "download", fake_download)

    def grab():
        resp = client.post("/bucket/audio/from-url", json={"url": "https://youtu.be/abc"})
        assert resp.status_code == 202, resp.text
        return resp.json()

    track = grab()
    assert (track["status"], track["to_bucket"]) == ("processing", True)
    assert client.enqueued["audio_link"] == [track["id"]]
    downloads.download_audio_track(track["id"])  # what the downloads worker runs
    assert s3.objects == {"clipforge/audio/Lo-fi_beats_study.m4a": (b"converted webm", "audio/mp4")}
    assert not get_storage().path_for(f"audio/{track['id']}").exists()  # nothing kept here
    (done,) = client.get("/audio").json()  # in the music library, its audio in the bucket
    assert (done["id"], done["status"], done["filename"], done["duration_s"]) == (
        track["id"], "ready", "Lo-fi_beats_study.m4a", 12.5
    )  # fmt: skip
    assert done["bucket"]["key"] == "clipforge/audio/Lo-fi_beats_study.m4a"
    # Tagged as converted already: it is used as it is, not converted again.
    assert s3.metadata["clipforge/audio/Lo-fi_beats_study.m4a"] == {"clipforge": "converted-music"}

    # The bucket refuses: it says so, and nothing is kept here either.
    def refused(*args, **kwargs):
        raise RuntimeError("access denied")

    monkeypatch.setattr(s3, "upload_file", refused)
    track = grab()
    downloads.download_audio_track(track["id"])
    failed = next(t for t in client.get("/audio").json() if t["id"] == track["id"])
    assert failed["status"] == "failed"
    assert failed["error"].startswith("Could not save it to the bucket")
    assert not get_storage().path_for(f"audio/{track['id']}").exists()
    assert client.delete(f"/audio/{track['id']}").status_code == 204  # and it can be removed


def test_a_link_needs_a_bucket(client, monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "")
    get_settings.cache_clear()
    resp = client.post("/bucket/audio/from-url", json={"url": "https://youtu.be/abc"})
    assert resp.status_code == 409 and client.get("/audio").json() == []


def test_every_audio_file_in_the_bucket_is_listed(client, s3):  # noqa: F811
    s3.objects.update(
        {
            "clipforge/audio/Calm.m4a": (b"a", "audio/mp4"),
            "clipforge/uploads/trip.mp4": (b"a video", "video/mp4"),
            "old/songs/Wedding.MP3": (b"b", "audio/mpeg"),
            "voice.wav": (b"c", "audio/wav"),
            "notes.txt": (b"d", "text/plain"),
        }
    )
    body = client.get("/bucket/audio").json()
    assert body["truncated"] is False
    assert [(a["folder"], a["name"]) for a in body["audio"]] == [  # newest first
        ("", "voice.wav"),
        ("old/songs/", "Wedding.MP3"),
        ("clipforge/audio/", "Calm.m4a"),
    ]
    resp = client.get("/bucket/audio/download", params={"key": "old/songs/Wedding.MP3"},
                      follow_redirects=False)  # fmt: skip
    disposition = parse_qs(urlsplit(resp.headers["location"]).query)
    assert disposition["response-content-disposition"][0].startswith(
        'attachment; filename="Wedding.MP3"'
    )
    resp = client.get("/bucket/audio/link", params={"key": "voice.wav"}, follow_redirects=False)
    assert resp.headers["location"] == "https://signed.example/my-reels/voice.wav"


def test_rename_keeps_the_folder_and_extension(client, s3, monkeypatch):  # noqa: F811
    monkeypatch.setattr(bgaudio, "prepare_track", _fake_prepare)
    key = "clipforge/audio/My_song.m4a"
    s3.objects[key] = (b"aac", "audio/mp4")
    track = _bring(client, key)

    def rename(key, name):
        return client.post("/bucket/audio/rename", json={"key": key, "name": name})

    resp = rename(key, "Morning tune")
    assert resp.json() == {"key": "clipforge/audio/Morning_tune.m4a", "name": "Morning_tune.m4a"}
    assert s3.objects == {"clipforge/audio/Morning_tune.m4a": (b"aac", "audio/mp4")}
    # The library knows where its bucket file is now, and shows its new name.
    assert _copy(client, track["id"])["key"] == "clipforge/audio/Morning_tune.m4a"
    assert client.get("/audio").json()[0]["filename"] == "Morning_tune.m4a"

    # Typed with the extension, or a Hindi name: no double ".m4a".
    key = rename("clipforge/audio/Morning_tune.m4a", "सुबह की धुन.m4a").json()["key"]
    assert key == "clipforge/audio/सुबह_की_धुन.m4a"
    assert rename(key, "सुबह की धुन").json()["key"] == key  # the same name: nothing to do

    # A name in use is never replaced.
    s3.objects["clipforge/audio/Calm.m4a"] = (b"other", "audio/mp4")
    resp = rename(key, "Calm")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "name_taken"
    assert s3.objects["clipforge/audio/Calm.m4a"] == (b"other", "audio/mp4") and key in s3.objects


def test_deleted_from_the_bucket_it_leaves_the_music(client, db_session, s3):  # noqa: F811
    s3.objects["clipforge/audio/Calm.m4a"] = (b"aac", "audio/mp4")
    track = _bring(client, "clipforge/audio/Calm.m4a")
    reel = Reel(
        user_id=db_session.get(AudioTrack, track["id"]).user_id,
        platform="instagram",
        language="hi",
        voice="hf_alpha",
        topic="t",
        duration_s=30,
        music_id=track["id"],
    )
    db_session.add(reel)  # fmt: skip
    db_session.commit()
    resp = client.delete("/bucket/audio", params={"key": "clipforge/audio/Calm.m4a"})
    assert resp.status_code == 204
    assert s3.objects == {} and client.get("/audio").json() == []  # its audio is gone
    db_session.refresh(reel)
    assert reel.music_id is None  # its next render has no music

    # A track from before, still on this computer, is kept.
    old = AudioTrack(user_id=reel.user_id, filename="Old.m4a", status="ready",
                     bucket_copy={"key": "clipforge/audio/Old.m4a", "version": 1,
                                  "uploaded_at": "2026-10-01T00:00:00+00:00"})  # fmt: skip
    db_session.add(old)
    db_session.commit()
    old.path = str(get_storage().path_for(f"audio/{old.id}/track.m4a"))
    Path(old.path).parent.mkdir(parents=True)
    Path(old.path).write_bytes(b"aac")
    db_session.commit()
    s3.objects["clipforge/audio/Old.m4a"] = (b"aac", "audio/mp4")
    client.delete("/bucket/audio", params={"key": "clipforge/audio/Old.m4a"})
    (kept,) = client.get("/audio").json()
    assert kept["id"] == old.id and kept["bucket"] is None


def test_only_audio_files_are_touched(client, s3):  # noqa: F811
    s3.objects["clipforge/uploads/trip.mp4"] = (b"a video", "video/mp4")
    for key in ("clipforge/uploads/trip.mp4", "notes.txt", "clipforge/../x.m4a"):
        for action in ("link", "download"):
            assert client.get(f"/bucket/audio/{action}", params={"key": key}).status_code == 400
        assert client.delete("/bucket/audio", params={"key": key}).status_code == 400
        resp = client.post("/bucket/audio/rename", json={"key": key, "name": "x"})
        assert resp.status_code == 400
    assert list(s3.objects) == ["clipforge/uploads/trip.mp4"] and s3.deleted == []


def test_aws_links_use_the_buckets_own_region():
    """A signed link to the global address fails for a new bucket outside
    us-east-1 (SignatureDoesNotMatch); other providers keep their address."""
    bucket._make_client.cache_clear()
    aws = bucket._make_client(None, "ap-south-1", "AKIAEXAMPLE", "secret")
    url = aws.generate_presigned_url("get_object", Params={"Bucket": "my-reels", "Key": "k.mp4"})
    assert url.startswith("https://my-reels.s3.ap-south-1.amazonaws.com/k.mp4?")
    r2 = bucket._make_client("http://minio:9000", "us-east-1", "key", "secret")
    url = r2.generate_presigned_url("get_object", Params={"Bucket": "my-reels", "Key": "k.mp4"})
    assert url.startswith("http://minio:9000/my-reels/k.mp4?")
    bucket._make_client.cache_clear()


def test_a_file_in_the_bucket_is_added_to_the_music(client, s3):  # noqa: F811
    s3.objects["old/songs/Wedding.mp3"] = (b"mp3", "audio/mpeg")
    resp = client.post("/audio/from-bucket", json={"key": "old/songs/Wedding.mp3"})
    assert resp.status_code == 201, resp.text
    t = resp.json()
    assert (t["filename"], t["status"], t["size_bytes"]) == ("Wedding.mp3", "ready", 3)
    assert t["bucket"]["key"] == "old/songs/Wedding.mp3" and t["bucket"]["current"] is True
    # Nothing is downloaded: it plays from the bucket and is fetched while a
    # clip or reel is made.
    assert not get_storage().path_for(f"audio/{t['id']}").exists()
    played = client.get(f"/audio/{t['id']}/file", follow_redirects=False)
    assert played.headers["location"] == "https://signed.example/my-reels/old/songs/Wedding.mp3"
    # Picked again: the same track, not fetched twice.
    again = client.post("/audio/from-bucket", json={"key": "old/songs/Wedding.mp3"}).json()
    assert again["id"] == t["id"] and len(client.get("/audio").json()) == 1
    assert s3.objects == {
        "old/songs/Wedding.mp3": (b"mp3", "audio/mpeg")
    }  # the bucket is untouched


def test_adding_bucket_music_can_fail_cleanly(client, s3, monkeypatch):  # noqa: F811
    def bring(key):
        return client.post("/audio/from-bucket", json={"key": key})

    resp = bring("gone.mp3")  # deleted from the bucket meanwhile
    assert resp.status_code == 502
    assert resp.json()["error"]["message"].startswith("That file is not in the bucket any more")

    s3.objects["big.wav"] = (b"x" * 11, "audio/wav")
    monkeypatch.setenv("MAX_AUDIO_UPLOAD_BYTES", "10")
    get_settings.cache_clear()
    assert bring("big.wav").status_code == 413
    monkeypatch.delenv("MAX_AUDIO_UPLOAD_BYTES")
    get_settings.cache_clear()

    for key in ("clipforge/uploads/trip.mp4", "notes.txt", "clipforge/../x.m4a"):
        assert bring(key).status_code == 400
    assert client.get("/audio").json() == []  # nothing half-made is left
