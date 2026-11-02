"""The "Bucket" section: videos from this computer go to the bucket in parts,
into their own folder (<prefix>/uploads/clips/ for "Clip a video",
<prefix>/uploads/reels/ for "Create a reel"), never replacing another file; every
video in the bucket can be opened, downloaded and deleted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from app.config.settings import get_settings
from app.services import bucket

boto_errors = pytest.importorskip("botocore.exceptions")


class FakeS3:
    """Enough of an S3 client for uploads (in parts or whole), HEAD, listing,
    copying and deleting."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}  # key -> (data, content type)
        self.metadata: dict[str, dict[str, str]] = {}  # key -> its "x-amz-meta-" tags
        self.started: dict[str, dict] = {}  # upload id -> {"key", "type", "parts"}
        self.aborted: list[str] = []
        self.deleted: list[str] = []
        self.head_status: int | None = None  # e.g. 403: these keys may not look
        self.deny_delete = False  # keys without s3:DeleteObject

    def head_object(self, Bucket, Key):
        status = self.head_status or (200 if Key in self.objects else 404)
        if status != 200:
            reply = {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}
            raise boto_errors.ClientError(reply, "HeadObject")
        return {"ContentLength": len(self.objects[Key][0]), "Metadata": self.metadata.get(Key, {})}

    def create_multipart_upload(self, Bucket, Key, ContentType):
        upload_id = f"up-{len(self.started) + 1}"
        self.started[upload_id] = {"key": Key, "type": ContentType, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        assert self.started[UploadId]["key"] == Key
        self.started[UploadId]["parts"][PartNumber] = Body
        return {"ETag": f'"etag-{PartNumber}"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        up = self.started.pop(UploadId)
        numbers = sorted(up["parts"])
        assert MultipartUpload["Parts"] == [
            {"PartNumber": n, "ETag": f'"etag-{n}"'} for n in numbers
        ]
        self.objects[Key] = (b"".join(up["parts"][n] for n in numbers), up["type"])

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.started.pop(UploadId)
        self.aborted.append(Key)

    def upload_file(self, path, bucket_name, key, ExtraArgs):
        self.objects[key] = (Path(path).read_bytes(), ExtraArgs["ContentType"])
        self.metadata[key] = ExtraArgs.get("Metadata", {})

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]
        self.metadata[Key] = self.metadata.get(CopySource["Key"], {})  # S3 copies the tags

    def download_file(self, Bucket, Key, Filename):
        Path(Filename).write_bytes(self.objects[Key][0])

    def delete_object(self, Bucket, Key):
        if self.deny_delete:
            raise boto_errors.ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")
        self.objects.pop(Key, None)  # S3: no error if it isn't there
        self.metadata.pop(Key, None)
        self.deleted.append(Key)

    def get_paginator(self, operation):
        start = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
        objects = [  # written one minute apart, in order
            {"Key": k, "Size": len(data), "LastModified": start + timedelta(minutes=i)}
            for i, (k, (data, _)) in enumerate(self.objects.items())
        ]

        class Paginator:  # pages of 2, to check every page is read
            def paginate(self, Bucket):
                for i in range(0, max(len(objects), 1), 2):
                    yield {"Contents": objects[i : i + 2], "IsTruncated": i + 2 < len(objects)}

        return Paginator()

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        url = f"https://signed.example/{Params['Bucket']}/{Params['Key']}"
        if "ResponseContentDisposition" in Params:  # S3 sends it back as Content-Disposition
            url += "?" + urlencode(
                {"response-content-disposition": Params["ResponseContentDisposition"]}
            )
        return url


@pytest.fixture
def s3(monkeypatch):
    # Set every BUCKET_* here: inside Docker the real .env is in the environment.
    for name, value in {"NAME": "my-reels", "PREFIX": "clipforge", "PUBLIC_URL": ""}.items():
        monkeypatch.setenv(f"BUCKET_{name}", value)
    get_settings.cache_clear()
    fake = FakeS3()
    monkeypatch.setattr(bucket, "_client", lambda settings: fake)
    return fake


def _send(client, filename: str, pieces: list[bytes], **folder) -> dict:
    """What the page does: start, one PUT per piece, complete."""
    start = client.post(
        "/bucket/uploads",
        json={"filename": filename, "size_bytes": sum(map(len, pieces)), **folder},
    )
    assert start.status_code == 200, start.text
    ref = {k: start.json()[k] for k in ("key", "upload_id")}
    parts = []
    for n, piece in enumerate(pieces, start=1):
        resp = client.put("/bucket/uploads/part", params={**ref, "number": n}, content=piece)
        assert resp.status_code == 200, resp.text
        parts.append(resp.json())
    done = client.post("/bucket/uploads/complete", json={**ref, "parts": parts[::-1]})
    assert done.status_code == 200, done.text
    return {**start.json(), **done.json()}


def test_status_shows_where_videos_go(client, s3):
    assert client.get("/bucket").json() == {
        "set_up": True,
        "bucket": "my-reels",
        "endpoint": "AWS S3",
        "uploads_folder": "clipforge/uploads/",
        "upload_folders": {
            "clips": "clipforge/uploads/clips/",
            "reels": "clipforge/uploads/reels/",
        },
        "reels_folders": ["clipforge/reels/", "clipforge/clips/"],
        "audio_folder": "clipforge/audio/",
    }


def test_video_goes_up_in_parts_into_its_own_folder(client, s3):
    sent = _send(client, "My trip.mp4", [b"first-", b"second-", b"last"])
    assert sent["key"] == "clipforge/uploads/clips/My_trip.mp4"
    assert sent["part_size"] == bucket.PART_SIZE
    assert s3.objects["clipforge/uploads/clips/My_trip.mp4"] == (b"first-second-last", "video/mp4")

    videos = client.get("/bucket/videos").json()["videos"]
    assert [(v["name"], v["folder"], v["size_bytes"]) for v in videos] == [
        ("My_trip.mp4", "clipforge/uploads/clips/", 17)
    ]
    resp = client.get("/bucket/videos/link", params={"key": sent["key"]}, follow_redirects=False)
    assert resp.status_code == 307
    assert (
        resp.headers["location"]
        == "https://signed.example/my-reels/clipforge/uploads/clips/My_trip.mp4"
    )


def test_the_part_of_the_app_picked_is_the_folder(client, s3):
    reel = _send(client, "My trip.mp4", [b"a reel"], folder="reels")["key"]
    clip = _send(client, "My trip.mp4", [b"a clip"], folder="clips")["key"]
    # Each folder has its own names: the same name is free in the other.
    assert reel == "clipforge/uploads/reels/My_trip.mp4"
    assert clip == "clipforge/uploads/clips/My_trip.mp4"
    assert [v["folder"] for v in client.get("/bucket/videos").json()["videos"]] == [
        "clipforge/uploads/clips/",
        "clipforge/uploads/reels/",
    ]
    for folder in ("uploads", "../reels", ""):
        resp = client.post(
            "/bucket/uploads", json={"filename": "a.mp4", "size_bytes": 3, "folder": folder}
        )
        assert resp.status_code == 422, folder
    assert s3.started == {}


def _download(client, key: str) -> tuple[str, str]:
    """(signed link without the query, the Content-Disposition S3 will send)."""
    resp = client.get("/bucket/videos/download", params={"key": key}, follow_redirects=False)
    assert resp.status_code == 307, resp.text
    url = urlsplit(resp.headers["location"])
    return (
        f"{url.scheme}://{url.netloc}{url.path}",
        parse_qs(url.query)["response-content-disposition"][0],
    )


def test_download_saves_the_video_under_its_name(client, s3):
    key = _send(client, "My trip.mp4", [b"video"])["key"]
    link, disposition = _download(client, key)
    assert link == "https://signed.example/my-reels/clipforge/uploads/clips/My_trip.mp4"
    assert disposition == "attachment; filename=\"My_trip.mp4\"; filename*=UTF-8''My_trip.mp4"


def test_zip_file_goes_up_and_is_listed_with_the_videos(client, s3):
    sent = _send(client, "Garden_approved.zip", [b"PK-", b"zip"])
    assert sent["key"] == "clipforge/uploads/clips/Garden_approved.zip"
    assert s3.objects[sent["key"]] == (b"PK-zip", "application/zip")
    _send(client, "My trip.mp4", [b"video"])
    videos = client.get("/bucket/videos").json()["videos"]
    assert [v["name"] for v in videos] == ["My_trip.mp4", "Garden_approved.zip"]

    _, disposition = _download(client, sent["key"])
    assert disposition.startswith('attachment; filename="Garden_approved.zip"')
    hindi = _send(client, "बगीचा.zip", [b"zip"])["key"]
    assert _download(client, hindi)[1].startswith('attachment; filename="files.zip"')

    assert client.delete("/bucket/videos", params={"key": sent["key"]}).status_code == 204
    assert sent["key"] not in s3.objects
    # Not a music file.
    start = client.post(
        "/bucket/uploads", json={"filename": "songs.zip", "size_bytes": 3, "folder": "audio"}
    )
    assert start.json()["error"]["code"] == "unsupported_type"


def test_download_of_a_hindi_name_keeps_it(client, s3):
    key = _send(client, "जीवन बदलने वाले.mp4", [b"video"])["key"]
    assert key == "clipforge/uploads/clips/जीवन_बदलने_वाले.mp4"
    _, disposition = _download(client, key)
    # Browsers take filename*; the plain filename is for old ones (ASCII only).
    assert disposition == (
        'attachment; filename="video.mp4"; '
        "filename*=UTF-8''%E0%A4%9C%E0%A5%80%E0%A4%B5%E0%A4%A8_%E0%A4%AC%E0%A4%A6%E0%A4%B2%E0%A4%A8"
        "%E0%A5%87_%E0%A4%B5%E0%A4%BE%E0%A4%B2%E0%A5%87.mp4"
    )


def test_every_video_in_the_bucket_is_listed_and_can_be_downloaded(client, s3, monkeypatch):
    s3.objects.update(
        {
            "clipforge/reels/Morning-1a2b3c4d.mp4": (b"a topic reel", "video/mp4"),
            "clipforge/reels/Morning-1a2b3c4d.srt": (b"captions", "application/x-subrip"),
            "clipforge/clips/Garden/youtube_shorts/part-001-5e6f.mp4": (b"a trim", "video/mp4"),
            "old-videos/2024/Wedding.MOV": (b"not from this app", "video/quicktime"),
            "notes.txt": (b"not a video", "text/plain"),
            "intro.webm": (b"at the top", "video/webm"),
        }
    )
    _send(client, "My trip.mp4", [b"mine"])
    body = client.get("/bucket/videos").json()
    assert body["truncated"] is False
    assert [(v["folder"], v["name"]) for v in body["videos"]] == [  # newest first
        ("clipforge/uploads/clips/", "My_trip.mp4"),
        ("", "intro.webm"),
        ("old-videos/2024/", "Wedding.MOV"),
        ("clipforge/clips/Garden/youtube_shorts/", "part-001-5e6f.mp4"),
        ("clipforge/reels/", "Morning-1a2b3c4d.mp4"),
    ]
    link, disposition = _download(client, "old-videos/2024/Wedding.MOV")
    assert link == "https://signed.example/my-reels/old-videos/2024/Wedding.MOV"
    assert disposition.startswith('attachment; filename="Wedding.MOV"')

    # A huge bucket: the list stops after MAX_LISTED objects and says so.
    monkeypatch.setattr(bucket, "MAX_LISTED", 3)
    body = client.get("/bucket/videos").json()
    assert body["truncated"] is True and len(body["videos"]) == 3


def test_a_video_is_deleted_from_the_bucket(client, s3):
    clip = "clipforge/clips/Garden/youtube_shorts/part-001-5e6f.mp4"
    s3.objects.update(
        {
            clip: (b"a trim", "video/mp4"),
            clip.removesuffix(".mp4") + ".srt": (b"captions", "application/x-subrip"),
            "old-videos/Wedding.MOV": (b"not from this app", "video/quicktime"),
        }
    )
    mine = _send(client, "My trip.mp4", [b"mine"])["key"]
    assert client.delete("/bucket/videos", params={"key": mine}).status_code == 204
    # A clip goes with its captions.
    assert client.delete("/bucket/videos", params={"key": clip}).status_code == 204
    assert s3.deleted == [mine, clip, clip.removesuffix(".mp4") + ".srt"]
    videos = client.get("/bucket/videos").json()["videos"]
    assert [v["key"] for v in videos] == ["old-videos/Wedding.MOV"]
    # Already gone (deleted in another tab): nothing to do.
    assert client.delete("/bucket/videos", params={"key": mine}).status_code == 204


def test_keys_that_may_not_delete_are_told_what_to_add(client, s3):
    s3.objects["clipforge/uploads/a.mp4"] = (b"x", "video/mp4")
    s3.deny_delete = True
    resp = client.delete("/bucket/videos", params={"key": "clipforge/uploads/a.mp4"})
    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == (
        "These keys may not delete from 'my-reels'. Add s3:DeleteObject to their policy "
        "(IAM > Users > the user > Permissions)."
    )
    assert "clipforge/uploads/a.mp4" in s3.objects


def test_a_name_in_use_is_never_replaced(client, s3):
    s3.objects["clipforge/reels/clip.mov"] = (b"a reel", "video/quicktime")  # another folder
    first = _send(client, "clip.mov", [b"one"])["key"]
    second = _send(client, "clip.mov", [b"two"])["key"]
    third = _send(client, "clip.mov", [b"three"])["key"]
    assert (first, second, third) == (
        "clipforge/uploads/clips/clip.mov",
        "clipforge/uploads/clips/clip-2.mov",
        "clipforge/uploads/clips/clip-3.mov",
    )
    assert s3.objects[first] == (b"one", "video/quicktime")
    videos = client.get("/bucket/videos").json()["videos"]
    assert [v["name"] for v in videos] == ["clip-3.mov", "clip-2.mov", "clip.mov", "clip.mov"]
    assert videos[-1]["folder"] == "clipforge/reels/"


def test_without_permission_to_look_the_name_gets_the_time(client, s3):
    s3.head_status = 403
    key = _send(client, "clip.mp4", [b"x"])["key"]
    assert key.startswith("clipforge/uploads/clips/clip-") and key.endswith(".mp4")
    assert len(key) == len("clipforge/uploads/clips/clip-20261028-120000.mp4")


def test_cancel_drops_the_pieces_sent(client, s3):
    start = client.post("/bucket/uploads", json={"filename": "big.mkv", "size_bytes": 10}).json()
    ref = {"key": start["key"], "upload_id": start["upload_id"]}
    client.put("/bucket/uploads/part", params={**ref, "number": 1}, content=b"12345")
    assert client.post("/bucket/uploads/abort", json=ref).status_code == 204
    assert s3.aborted == ["clipforge/uploads/clips/big.mkv"] and s3.objects == {}


def test_uploads_only_write_videos_to_the_uploads_folder(client, s3):
    def start(filename, size=10):
        return client.post("/bucket/uploads", json={"filename": filename, "size_bytes": size})

    assert start("notes.txt").json()["error"]["code"] == "unsupported_type"
    assert start("huge.mp4", bucket.PART_SIZE * bucket.MAX_PARTS + 1).status_code == 413
    for key in ("clipforge/reels/x.mp4", "clipforge/uploads/../reels/x.mp4"):
        params = {"key": key, "upload_id": "u", "number": 1}
        assert client.put("/bucket/uploads/part", params=params, content=b"x").status_code == 400
    # Open / Download / Delete: any video, but only videos.
    for key in ("notes.txt", "clipforge/reels/x.srt", "clipforge/../x.mp4"):
        for action in ("link", "download"):
            resp = client.get(f"/bucket/videos/{action}", params={"key": key})
            assert resp.status_code == 400, (action, key)
        assert client.delete("/bucket/videos", params={"key": key}).status_code == 400
    assert s3.deleted == []
    ref = {"key": "clipforge/uploads/a.mp4", "upload_id": "u"}
    assert client.put("/bucket/uploads/part", params={**ref, "number": 0}).status_code == 422
    assert client.put("/bucket/uploads/part", params={**ref, "number": 1}).status_code == 400
    assert s3.started == {} and s3.objects == {}


def test_without_a_bucket_the_section_says_so(client, monkeypatch):
    monkeypatch.setenv("BUCKET_NAME", "")
    get_settings.cache_clear()
    assert client.get("/bucket").json()["set_up"] is False
    resp = client.get("/bucket/videos")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "bucket_not_set_up"
