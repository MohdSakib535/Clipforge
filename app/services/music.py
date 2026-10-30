"""Background music lives in the bucket (<prefix>/audio/): a track in the
library is its name, length and bucket file, nothing on this computer. A clip
or reel being made fetches the music for that time only (``working_copy``):
converted like an upload, mixed in, deleted again."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import AudioTrack, Clip, Reel
from app.services import bgaudio, bucket
from app.services.storage import get_storage


class MusicError(RuntimeError):
    """The music couldn't be fetched from the bucket; the message says why."""


def bucket_key(track: AudioTrack | None) -> str | None:
    return (track.bucket_copy or {}).get("key") if track is not None else None


# The tag on bucket files the app converted itself (an upload or a link,
# already stereo AAC at the clips' loudness): they are used as they are.
# Converting a 4-minute song again took ~6 s; downloading it ~0.5 s.
CONVERTED = {"clipforge": "converted-music"}


def is_converted(metadata: dict[str, str] | None) -> bool:
    return (metadata or {}).get("clipforge") == CONVERTED["clipforge"]


def in_bucket(key: str, *, converted: bool) -> dict:
    """What a track keeps about its bucket file. No "version": there is no
    file here to compare it with."""
    return {
        "key": key,
        "version": None,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "converted": converted,
    }


@contextmanager
def working_copy(track: AudioTrack | None) -> Iterator[str | None]:
    """The track's audio on this computer while the block runs: a file kept
    here from before, or a copy of its bucket file (converted, unless the app
    converted it already), deleted after. None without (ready) music. Raises
    MusicError if it can't be fetched."""
    if track is None or track.status != "ready":
        yield None
        return
    if track.path and Path(track.path).exists():
        yield track.path
        return
    key = bucket_key(track)
    if not key:
        yield None
        return
    folder = get_storage().path_for(f"audio/{track.id}/work-{uuid.uuid4().hex[:12]}")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        try:
            if (track.bucket_copy or {}).get("converted"):
                bucket.download(key, folder / "track.m4a")
            else:
                raw = folder / f"source{Path(key).suffix.lower()}"
                bucket.download(key, raw)
                bgaudio.prepare_track(raw, folder / "track.m4a")
                raw.unlink()  # only the converted file is needed
        except Exception as exc:
            raise MusicError(
                f'Could not get the music "{track.filename}" from the bucket: {exc}'
            ) from exc
        yield str(folder / "track.m4a")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        try:
            folder.parent.rmdir()  # nothing else of this track here
        except OSError:
            pass


def forget(db: Session, track: AudioTrack) -> None:
    """Drop a track from the library (the caller commits): clips and reels
    keep the music already mixed into their files; the next render has none."""
    db.query(Clip).filter(Clip.bg_audio_id == track.id).update({Clip.bg_audio_id: None})
    db.query(Reel).filter(Reel.music_id == track.id).update({Reel.music_id: None})
    get_storage().delete(f"audio/{track.id}")
    db.delete(track)
