"""Fast "is this the same file?" fingerprint.

Hashing a whole 5 GB film takes long enough to be felt at upload. Instead we
hash the size plus three 4 MB samples (start, middle, end): two different
videos practically never share all of those, while the same file uploaded
again always does. Used to reuse a transcript instead of paying for it twice.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

SAMPLE_BYTES = 4 * 1024 * 1024


def file_fingerprint(path: str | Path) -> str:
    """``"<size>-<sha256 of samples>"``. The size comes first so a mismatch is
    visible without hashing anything."""
    size = os.path.getsize(path)
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as fh:
        if size <= 3 * SAMPLE_BYTES:
            h.update(fh.read())
        else:
            for offset in (0, size // 2 - SAMPLE_BYTES // 2, size - SAMPLE_BYTES):
                fh.seek(offset)
                h.update(fh.read(SAMPLE_BYTES))
    return f"{size}-{h.hexdigest()[:40]}"
