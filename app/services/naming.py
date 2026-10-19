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


def clip_basename(rank: int | None) -> str:
    """File name (no extension) for a downloaded clip: just the part number,
    e.g. ``part-041``. Zero-padded so parts sort in video order. The source
    video's name is left out on purpose (it is in the ZIP's .txt/.json)."""
    return f"part-{rank or 0:03d}"
