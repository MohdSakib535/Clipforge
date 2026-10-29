from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BucketCopy(BaseModel):
    """A reel's copy in the bucket (POST .../upload-to-bucket), or the bucket
    file a music track was brought in from (POST /audio/from-bucket)."""

    key: str  # where it is in the bucket
    uploaded_at: datetime
    # False: the video changed after the upload (made again, colour, music);
    # uploading again replaces the copy.
    current: bool


# --- The "Bucket" section: videos from this computer -> <prefix>/uploads/ ---
class BucketStatus(BaseModel):
    set_up: bool  # BUCKET_NAME is set
    bucket: str
    endpoint: str  # "AWS S3" or BUCKET_ENDPOINT_URL
    uploads_folder: str  # where the section's videos go, e.g. "clipforge/uploads/"
    reels_folders: list[str]  # where "Upload to bucket" puts reels (kept apart)
    audio_folder: str  # where music goes, e.g. "clipforge/audio/"


class BucketUploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(gt=0)
    folder: Literal["uploads", "audio"] = "uploads"  # a video, or a music file


class BucketUploadRef(BaseModel):
    key: str = Field(min_length=1, max_length=1024)
    upload_id: str = Field(min_length=1, max_length=1024)


class BucketUploadStart(BucketUploadRef):
    part_size: int  # send the file in pieces of this many bytes (the last may be smaller)


class BucketPart(BaseModel):
    number: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=256)


class BucketUploadDone(BucketUploadRef):
    parts: list[BucketPart] = Field(min_length=1)


class BucketFile(BaseModel):
    key: str
    name: str  # the file's name
    folder: str  # e.g. "clipforge/uploads/"; "" at the top of the bucket
    size_bytes: int
    uploaded_at: datetime


class BucketVideos(BaseModel):
    videos: list[BucketFile]  # every video in the bucket, newest first
    truncated: bool  # a very big bucket: only part of it was looked at


class BucketAudio(BaseModel):
    audio: list[BucketFile]  # every audio file in the bucket, newest first
    truncated: bool


class BucketRename(BaseModel):
    key: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=200)  # the new name; the extension stays


class BucketRenamed(BaseModel):
    key: str  # where it is now
    name: str
