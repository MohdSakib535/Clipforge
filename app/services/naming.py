from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def _keep(ch: str) -> bool:
    # Letters/digits from any script, plus combining marks: Hindi vowel signs
    # (matras) are marks, so a plain \w would turn "पोप" into "प_प".
    return ch.isalnum() or ch == "-" or unicodedata.category(ch).startswith("M")


def video_stem(video_filename: str) -> str:
    """Filesystem-safe version of the source video's name (no extension)."""
    stem = Path(video_filename or "").stem
    stem = re.sub(r"_+", "_", "".join(ch if _keep(ch) else "_" for ch in stem))
    return stem.strip("_-")[:60] or "video"


def clip_basename(video_filename: str, platform: str, rank: int | None) -> str:
    """File name (no extension) for a downloaded clip, e.g.
    ``The_Popes_Exorcist_part-041_facebook_video`` - the source video's name and
    the part number, so a clip can be matched to its place in the video.

    Parts are numbered in video order, zero-padded so they sort correctly."""
    return f"{video_stem(video_filename)}_part-{rank or 0:03d}_{platform}"
