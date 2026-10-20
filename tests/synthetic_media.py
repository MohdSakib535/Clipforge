"""Synthetic test media for pipeline tests (no third-party content).

Audio is a melody of pseudo-random notes (one every 0.25 s plus a slower bass
voice); video is a 4x3 grid of grey blocks whose brightness changes every
0.5 s. Both are non-repeating, so an alignment can only fit one way, and a
different ``seed`` gives unrelated media.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _melody(seed: int) -> str:
    return (
        f"0.4*sin(2*PI*t*220*pow(2,mod(floor(abs(sin((floor(t*4)+1)*12.9898*{seed})"
        f"*43758.5453)),12)/12))+0.25*sin(2*PI*t*110*pow(2,mod(floor(abs(sin("
        f"(floor(t*2)+3)*78.233*{seed})*12345.678)),12)/12))"
    )


def _blocks(seed: int) -> str:
    return (
        f"255*mod(abs(sin((floor(X/40)+7*floor(Y/30)+13*floor(T*2)+1)*12.9898*{seed})"
        f"*43758.5453),1)"
    )


def make_media(path: Path, seed: int, seconds: float, *, audio: bool = True, video: bool = True):
    """MP4 (or audio-only file if ``video`` is False) of synthetic content."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if video:
        cmd += [
            "-f", "lavfi", "-i",
            f"color=c=black:s=160x90:r=10:d={seconds},format=gray,geq=lum='{_blocks(seed)}'",
        ]  # fmt: skip
    if audio:
        cmd += ["-f", "lavfi", "-i", f"aevalsrc='{_melody(seed)}':s=22050:d={seconds}"]
    if video:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    subprocess.run([*cmd, "-shortest", str(path)], check=True, capture_output=True)
    return path
