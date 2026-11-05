"""Upload to a bucket: AWS S3 or any S3-compatible storage (Cloudflare R2,
MinIO, DigitalOcean Spaces, Backblaze B2, Google Cloud Storage), set up with
the BUCKET_* settings. Finished reels go to reels/ and clips/; videos sent from
the "Bucket" section to their own folder, uploads/clips/ or uploads/reels/ (the
part of the app they are for); music from the library to audio/. Any video
in the bucket can be listed, opened, downloaded and deleted; any audio file
renamed too. boto3 is imported on first use."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from itertools import count
from pathlib import Path
from urllib.parse import quote

from app.config.settings import Settings, get_settings
from app.services.naming import video_stem

LINK_TTL_S = 7 * 24 * 3600  # the longest a signed S3 link may last
UPLOADS = "uploads"  # the "Bucket" section's folder, apart from reels/ and clips/
AUDIO = "audio"  # music from the library, apart from the videos
# A video from the computer goes up in parts of this size (the last one may
# be smaller); S3 allows at most MAX_PARTS of them.
PART_SIZE = 16 * 1024 * 1024
MAX_PARTS = 10_000
# Objects looked at for the list of videos (1,000 per request); a bigger
# bucket's list is cut off there.
MAX_LISTED = 100_000
_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
}
VIDEO_EXTENSIONS = frozenset(_VIDEO_TYPES)  # what counts as a video (listing, uploads)
_AUDIO_TYPES = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
}
AUDIO_EXTENSIONS = frozenset(_AUDIO_TYPES)  # what counts as audio (listing)
# ZIP files (e.g. a downloaded ZIP of clips) go up and are listed with the videos.
_ZIP_TYPES = {".zip": "application/zip"}
ZIP_EXTENSIONS = frozenset(_ZIP_TYPES)
_CONTENT_TYPES = {
    **_VIDEO_TYPES,
    **_AUDIO_TYPES,
    **_ZIP_TYPES,
    ".srt": "application/x-subrip",
}

# What S3 error codes mean for the user ({bucket} = BUCKET_NAME).
_WRONG_KEYS = "The bucket keys are wrong. Check BUCKET_ACCESS_KEY_ID and BUCKET_SECRET_ACCESS_KEY."
_REASONS = {
    "NoSuchBucket": "There is no bucket '{bucket}'. Check BUCKET_NAME (and BUCKET_REGION / "
    "BUCKET_ENDPOINT_URL).",
    "AccessDenied": "The bucket said access denied: these keys need permission to read, write "
    "and list '{bucket}'.",
    "InvalidAccessKeyId": _WRONG_KEYS,
    "SignatureDoesNotMatch": _WRONG_KEYS,
    "NoSuchKey": "That file is not in the bucket any more. Refresh the list.",
    "404": "That file is not in the bucket any more. Refresh the list.",  # HEAD has no body
}
_NO_DELETE = (
    "These keys may not delete from '{bucket}'. Add s3:DeleteObject to their policy "
    "(IAM > Users > the user > Permissions)."
)


class BucketError(Exception):
    """The bucket can't do what was asked; the message tells the user why."""


class BucketNotSetUp(BucketError):
    pass


class NameTaken(BucketError):
    """Rename: another file already has that name."""


def _settings() -> Settings:
    settings = get_settings()
    if not settings.bucket_name.strip():
        raise BucketNotSetUp(
            "No bucket is set up. Add BUCKET_NAME and its keys to .env, then run: "
            "docker compose up -d api"
        )
    return settings


def ensure_set_up() -> None:
    """BucketNotSetUp unless BUCKET_NAME is set."""
    _settings()


def _client(settings: Settings):
    return _make_client(
        settings.bucket_endpoint_url.strip() or None,
        settings.bucket_region.strip() or None,
        settings.bucket_access_key_id.strip() or None,
        settings.bucket_secret_access_key.strip() or None,
    )


@lru_cache(maxsize=4)  # one client per setup, reused (each part of a video is a call)
def _make_client(endpoint_url, region, access_key_id, secret_access_key):
    import boto3
    from botocore.config import Config

    return boto3.session.Session().client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            retries={"max_attempts": 3, "mode": "standard"},
            # Checksums only where S3 demands them: several S3-compatible
            # stores reject the extra checksum headers newer boto3 sends.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # AWS: the bucket's own address in its region (<bucket>.s3.<region>.
            # amazonaws.com). Signed links otherwise use the global one, which
            # fails (SignatureDoesNotMatch) for a new bucket outside us-east-1.
            s3={"addressing_style": "virtual"} if endpoint_url is None else None,
        ),
    )


def _explain(exc: Exception, settings: Settings, denied: str = "") -> str:
    """A message for the user, for what boto3 raised (``denied``: the one for
    access denied, if not the usual)."""
    from botocore.exceptions import (
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        NoCredentialsError,
    )

    known = (ClientError, ConnectTimeoutError, EndpointConnectionError, NoCredentialsError)
    cause: BaseException | None = exc
    while cause is not None and not isinstance(cause, known):  # upload_file wraps them
        cause = cause.__cause__ or cause.__context__
    if isinstance(cause, NoCredentialsError):
        return "No keys for the bucket. Set BUCKET_ACCESS_KEY_ID and BUCKET_SECRET_ACCESS_KEY."
    if isinstance(cause, ConnectTimeoutError | EndpointConnectionError):
        where = settings.bucket_endpoint_url.strip() or "AWS S3"
        return f"Could not reach the bucket ({where}). Check BUCKET_ENDPOINT_URL and the internet."
    if isinstance(cause, ClientError):
        code = cause.response.get("Error", {}).get("Code", "")
        reason = (code == "AccessDenied" and denied) or _REASONS.get(code)
        if reason:
            return reason.format(bucket=settings.bucket_name.strip())
    return f"The bucket request failed: {exc}"


@contextmanager
def _bucket(denied: str = "") -> Iterator[tuple]:
    """(client, bucket name); what boto3 raises becomes a BucketError."""
    settings = _settings()
    try:
        yield _client(settings), settings.bucket_name.strip()
    except BucketError:
        raise
    except Exception as exc:
        raise BucketError(_explain(exc, settings, denied)) from exc


def key_for(*parts: str) -> str:
    """Where a file goes in the bucket: under BUCKET_PREFIX, e.g.
    ``clipforge/reels/<name>.mp4``."""
    prefix = get_settings().bucket_prefix.strip().strip("/")
    return "/".join(p for p in (prefix, *parts) if p)


def uploads_folder(section: str = "") -> str:
    """The "Bucket" section's folder, e.g. ``clipforge/uploads/``; with a
    section ("clips": Clip a video, "reels": Create a reel), the folder of its
    videos, e.g. ``clipforge/uploads/clips/``. Files sent before there was a
    choice are at the top."""
    return key_for(UPLOADS, section) + "/"


def audio_folder() -> str:
    """Where music from the library goes, e.g. ``clipforge/audio/``."""
    return key_for(AUDIO) + "/"


def content_type(filename: str) -> str:
    return _CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def upload(path: Path, key: str, *, metadata: dict[str, str] | None = None) -> dict:
    """Copy ``path`` to ``key``, replacing what is there, with ``metadata``
    (S3 "x-amz-meta-" tags) if given. Returns what to save: {"key", "version",
    "uploaded_at"}; "version" is the file's mtime, so a file changed later
    (re-made, colour, music) shows as not uploaded."""
    version = path.stat().st_mtime_ns
    extra = {"ContentType": content_type(key)}
    if metadata:
        extra["Metadata"] = metadata
    with _bucket() as (client, name):
        client.upload_file(str(path), name, key, ExtraArgs=extra)
    return {"key": key, "version": version, "uploaded_at": datetime.now(UTC).isoformat()}


def head(key: str) -> dict:
    """About the file at ``key``: its size ("ContentLength") and "Metadata"."""
    with _bucket() as (client, name):
        return client.head_object(Bucket=name, Key=key)


def size(key: str) -> int:
    """How big the file at ``key`` is, in bytes."""
    return head(key)["ContentLength"]


def download(key: str, path: Path) -> None:
    """Save the file at ``key`` as ``path`` (on this computer)."""
    with _bucket() as (client, name):
        client.download_file(name, key, str(path))


def link(key: str) -> str:
    """An address that opens ``key``: on BUCKET_PUBLIC_URL if set, else a
    signed link that works for 7 days (the bucket can stay private)."""
    public = _settings().bucket_public_url.strip().rstrip("/")
    if public:
        return f"{public}/{quote(key)}"
    with _bucket() as (client, name):
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": name, "Key": key}, ExpiresIn=LINK_TTL_S
        )


def download_link(key: str) -> str:
    """A signed link (7 days) that saves ``key`` on the computer under its own
    name, instead of playing it in the browser."""
    name = key.rsplit("/", 1)[-1]
    ext = Path(name).suffix
    fallback = name.encode("ascii", "ignore").decode().replace('"', "")
    if not fallback.removesuffix(ext).strip("_-"):  # e.g. a Hindi name
        kind = "audio" if ext.lower() in AUDIO_EXTENSIONS else "video"
        fallback = ("files" if ext.lower() in ZIP_EXTENSIONS else kind) + ext
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"
    with _bucket() as (client, bucket_name):
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": key, "ResponseContentDisposition": disposition},
            ExpiresIn=LINK_TTL_S,
        )


def _taken(client, name: str, key: str) -> bool | None:
    """Is there a file at ``key``? None: these keys may not look (S3 answers
    403 instead of 404 without the list permission)."""
    from botocore.exceptions import ClientError

    try:
        client.head_object(Bucket=name, Key=key)
        return True
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in (403, 404):
            return None if status == 403 else False
        raise


def free_key(folder: str, stem: str, ext: str) -> str:
    """Where a file called ``stem + ext`` goes in ``folder``: its own name, or
    name-2, name-3... if that is taken, so an upload never replaces another."""
    with _bucket() as (client, name):
        for n in count(1):
            key = f"{folder}{stem}{f'-{n}' if n > 1 else ''}{ext}"
            taken = _taken(client, name, key)
            if taken is None:  # can't tell: a name made unique by the time
                return f"{folder}{stem}-{datetime.now(UTC):%Y%m%d-%H%M%S}{ext}"
            if not taken:
                return key


def start_upload(key: str) -> str:
    """Begin sending a file in parts (an S3 multipart upload); returns its id."""
    with _bucket() as (client, name):
        reply = client.create_multipart_upload(Bucket=name, Key=key, ContentType=content_type(key))
    return reply["UploadId"]


def upload_part(key: str, upload_id: str, number: int, data: bytes) -> str:
    """Send part ``number`` (1, 2, ...); returns its ETag, needed to finish."""
    with _bucket() as (client, name):
        reply = client.upload_part(
            Bucket=name, Key=key, UploadId=upload_id, PartNumber=number, Body=data
        )
    return reply["ETag"]


def finish_upload(key: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
    """Join the parts ((number, ETag), in order) into the file."""
    with _bucket() as (client, name):
        client.complete_multipart_upload(
            Bucket=name,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": n, "ETag": etag} for n, etag in parts]},
        )


def abort_upload(key: str, upload_id: str) -> None:
    """Drop the parts sent so far (the bucket keeps, and bills, them otherwise)."""
    with _bucket() as (client, name):
        client.abort_multipart_upload(Bucket=name, Key=key, UploadId=upload_id)


def delete(key: str) -> None:
    """Delete ``key`` from the bucket, with the captions (.srt) a clip went up
    with. Gone for good, unless the bucket keeps old versions."""
    keys = [key]
    if key.startswith(key_for("clips") + "/") and key.endswith(".mp4"):
        keys.append(key.removesuffix(".mp4") + ".srt")
    with _bucket(denied=_NO_DELETE) as (client, name):
        for k in keys:
            client.delete_object(Bucket=name, Key=k)  # no error if it's already gone


def rename(key: str, name: str) -> str:
    """Give ``key`` the file name ``name`` (its extension stays), in the same
    folder; returns the new key. Never replaces another file (NameTaken)."""
    folder, _, filename = key.rpartition("/")
    ext = Path(filename).suffix
    base = name.strip()
    if base.lower().endswith(ext.lower()):  # typed with the extension
        base = base[: -len(ext)]
    new_key = (f"{folder}/" if folder else "") + video_stem(base + ext) + ext
    if new_key == key:
        return key
    with _bucket() as (client, bucket_name):
        taken = _taken(client, bucket_name, new_key)
        if taken is not False:
            raise NameTaken(
                f"There is already a file called {new_key.rsplit('/', 1)[-1]} in this folder."
                if taken
                else "Could not check that name is free: these keys need permission to list."
            )
        client.copy_object(
            Bucket=bucket_name, Key=new_key, CopySource={"Bucket": bucket_name, "Key": key}
        )
    with _bucket(denied=_NO_DELETE) as (client, bucket_name):
        client.delete_object(Bucket=bucket_name, Key=key)
    return new_key


def list_videos() -> tuple[list[dict], bool]:
    """Every video and ZIP file in the bucket, in any folder, newest first:
    {"key", "name", "folder", "size_bytes", "uploaded_at"} ("folder" is "" at
    the top). Also whether the bucket has more than MAX_LISTED objects (the
    rest is left out)."""
    return _list(VIDEO_EXTENSIONS | ZIP_EXTENSIONS)


def list_audio() -> tuple[list[dict], bool]:
    """Every audio file in the bucket, in any folder, as list_videos()."""
    return _list(AUDIO_EXTENSIONS)


def _list(extensions: frozenset[str]) -> tuple[list[dict], bool]:
    files, seen, truncated = [], 0, False
    with _bucket() as (client, name):
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=name):
            contents = page.get("Contents", [])
            seen += len(contents)
            files += [o for o in contents if Path(o["Key"]).suffix.lower() in extensions]
            if seen >= MAX_LISTED and page.get("IsTruncated"):
                truncated = True
                break
    files.sort(key=lambda o: o["LastModified"], reverse=True)
    out = []
    for o in files:
        folder, _, filename = o["Key"].rpartition("/")
        out.append(
            {
                "key": o["Key"],
                "name": filename,
                "folder": f"{folder}/" if folder else "",
                "size_bytes": o["Size"],
                "uploaded_at": o["LastModified"],
            }
        )
    return out, truncated


def copy_out(saved: dict | None, version: int | None) -> dict | None:
    """The saved upload for the API, with "current": False once the local
    file (now at ``version``) is not the one that was uploaded."""
    if not saved:
        return None
    return {
        "key": saved["key"],
        "uploaded_at": saved["uploaded_at"],
        "current": version == saved["version"],
    }
