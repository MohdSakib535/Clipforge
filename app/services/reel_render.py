"""Put a reel together: one picture per scene with a slow zoom, the scene's
voice-over, word-by-word captions burned in, background music optional.

Layout of ``reels/<id>/``: pictures and voice clips are named after a hash of
what made them (``scene03-<hash>.png``, ``voice03-<hash>.wav``), so a new
render only redoes the scenes whose text or picture prompt changed; ``reel.mp4``
and ``thumb.jpg`` are the result. Everything else there is temporary.

Rendering is cheap (~1 s per scene); the time goes into the pictures.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.services import bgaudio, ffmpeg
from app.services.storage import get_storage

WIDTH, HEIGHT, FPS = 1080, 1920, 30
# Breath after each scene's line; the last one lingers so the call to action lands.
GAP_S = 0.25
END_GAP_S = 0.8
# Each picture zooms by this much over its scene (alternately in and out).
ZOOM = 0.15
VIDEO_NAME = "reel.mp4"
THUMB_NAME = "thumb.jpg"


def reel_dir(reel_id: str) -> Path:
    return get_storage().path_for(f"reels/{reel_id}")


def _key(*parts: object) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:12]


def scene_pictures(folder: Path, scenes: list[dict], settings: Settings) -> list[Path]:
    """Where each scene's picture is (or will be) for the current prompts."""
    return [
        picture_path(
            folder,
            i,
            sc["image_prompt"],
            settings.reel_image_width,
            settings.reel_image_height,
            settings.reel_image_steps,
        )  # fmt: skip
        for i, sc in enumerate(scenes)
    ]


def picture_path(
    reel_dir: Path, index: int, prompt: str, width: int, height: int, steps: int
) -> Path:
    return reel_dir / f"scene{index + 1:02d}-{_key(prompt, width, height, steps)}.png"


def voice_path(reel_dir: Path, index: int, text: str, language: str, voice: str) -> Path:
    return reel_dir / f"voice{index + 1:02d}-{_key(text, language, voice)}.wav"


def picture_prompt(prompt: str) -> str:
    # The model drew a 3-panel collage for "a montage of quick shots".
    suffix = "One single vertical 9:16 photo, not a collage, no text, no logos."
    return f"{prompt.rstrip('. ')}. {suffix}"


# ---------------------------------------------------------------------------
# Voice clips (16-bit mono WAV)
# ---------------------------------------------------------------------------
def write_wav(path: Path, samples, sample_rate: int) -> None:
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    os.replace(tmp, path)


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return data.astype(np.float32) / 32767, w.getframerate()


# ---------------------------------------------------------------------------
# Captions: 1-3 words at a time, timed by their length within the line
# ---------------------------------------------------------------------------
@dataclass
class Cue:
    start: float
    end: float
    text: str


_BREAK_AFTER = re.compile(r"[.!?।,;:]$")


def caption_cues(text: str, speech_s: float, *, max_words: int = 3, max_chars: int = 16):
    """Kokoro gives no word timings, so each chunk gets a share of the line's
    duration by its length: close enough for 1-3 words on screen."""
    chunks: list[list[str]] = []
    current: list[str] = []
    for word in text.split():
        if current and (len(current) >= max_words or len(" ".join([*current, word])) > max_chars):
            chunks.append(current)
            current = []
        current.append(word)
        if _BREAK_AFTER.search(word):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    weights = [sum(len(w) for w in c) + len(c) for c in chunks]
    total = sum(weights) or 1
    cues, t = [], 0.0
    for chunk, weight in zip(chunks, weights, strict=True):
        d = speech_s * weight / total
        cues.append(Cue(t, t + d, " ".join(chunk)))
        t += d
    return cues


def _ass_time(t: float) -> str:
    cs = round(max(0.0, t) * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(cues: list[Cue], font: str) -> str:
    """White bold text with a thick outline, a little "pop" as each chunk
    appears. It sits at ~70% height: above the caption/username area the apps
    draw at the bottom, below the middle of the picture."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {WIDTH}",
        f"PlayResY: {HEIGHT}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{font},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,7,2,2,110,110,560,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in cues:
        text = c.text.replace("\\", "").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{_ass_time(c.start)},{_ass_time(c.end)},Caption,,0,0,0,,"
            f"{{\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{text}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# FFmpeg commands (pure functions -> unit-testable)
# ---------------------------------------------------------------------------
def _filter_path(path: str) -> str:
    """Escape a file path for use inside a filtergraph option."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_scene_cmd(
    picture: str, dst: str, *, frames: int, zoom_in: bool, ass: str | None, preset: str
) -> list[str]:
    """One scene: the picture scaled to cover the frame (at twice the output
    size, so the zoom doesn't wobble), zoomed slowly, captions on top. Video
    only; the voice is added to the whole reel at once."""
    z = f"1+{ZOOM}*on/{frames}" if zoom_in else f"{1 + ZOOM}-{ZOOM}*on/{frames}"
    chain = (
        f"scale={2 * WIDTH}:{2 * HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={2 * WIDTH}:{2 * HEIGHT},"
        f"zoompan=z={z}:x=iw/2-(iw/zoom/2):y=ih/2-(ih/zoom/2):d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS}"
    )
    if ass:
        chain += f",ass={_filter_path(ass)}"
    chain += ",format=yuv420p"
    return [
        "ffmpeg", "-y",
        "-i", picture,
        "-vf", chain,
        "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", preset, "-crf", "20",
        "-an",
        dst,
    ]  # fmt: skip


def build_concat_cmd(list_file: str, dst: str) -> list[str]:
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", dst]


def build_mux_cmd(video: str, voice: str, dst: str, duration_s: float) -> list[str]:
    """Scenes + voice track; the voice gets the clips' loudness (-14 LUFS)."""
    return [
        "ffmpeg", "-y",
        "-i", video,
        "-i", voice,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-af", ffmpeg.AUDIO_NORM_FILTER,
        "-c:a", "aac", "-b:a", "128k",
        "-t", f"{duration_s:.3f}",
        "-movflags", "+faststart",
        dst,
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
@dataclass
class SceneInput:
    picture: Path
    text: str
    voice: Path  # WAV, mono


def render(
    reel_dir: Path,
    scenes: list[SceneInput],
    *,
    font: str,
    music: str | None = None,
    music_volume: int = 20,
    preset: str = "veryfast",
    workers: int = 4,
) -> float:
    """Write ``reel.mp4`` and ``thumb.jpg`` in ``reel_dir``; returns the length
    in seconds. Raises FFmpegError."""
    import numpy as np

    tmp = reel_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        # Each scene lasts its line plus a breath, rounded up to whole frames so
        # picture and voice stay in step over the whole reel.
        tracks, frames, speech, sample_rate = [], [], [], None
        for i, sc in enumerate(scenes):
            samples, sr = read_wav(sc.voice)
            if sample_rate not in (None, sr):
                raise ffmpeg.FFmpegError("voice clips have different sample rates")
            sample_rate = sr
            speech.append(len(samples) / sr)
            gap = END_GAP_S if i == len(scenes) - 1 else GAP_S
            n = math.ceil((speech[i] + gap) * FPS)
            pad = round(n / FPS * sr) - len(samples)
            tracks.append(np.concatenate([samples, np.zeros(max(0, pad), np.float32)]))
            frames.append(n)

        def _scene(i: int) -> str:
            sc = scenes[i]
            ass = tmp / f"captions{i + 1:02d}.ass"
            ass.write_text(build_ass(caption_cues(sc.text, speech[i]), font), encoding="utf-8")
            dst = tmp / f"scene{i + 1:02d}.mp4"
            ffmpeg.run(
                build_scene_cmd(
                    str(sc.picture), str(dst), frames=frames[i], zoom_in=i % 2 == 0,
                    ass=str(ass), preset=preset,
                )
            )  # fmt: skip
            return dst.name

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            names = list(pool.map(_scene, range(len(scenes))))

        list_file = tmp / "scenes.txt"
        list_file.write_text("".join(f"file '{n}'\n" for n in names))
        video = tmp / "video.mp4"
        ffmpeg.run(build_concat_cmd(str(list_file), str(video)))
        voice = tmp / "voice.wav"
        write_wav(voice, np.concatenate(tracks), sample_rate)

        duration = sum(frames) / FPS
        out = reel_dir / VIDEO_NAME
        voiced = tmp / "voiced.mp4"
        ffmpeg.run(build_mux_cmd(str(video), str(voice), str(voiced), duration))
        if music:
            mixed = tmp / "mixed.mp4"
            ffmpeg.run(
                bgaudio.build_mix_cmd(
                    str(voiced), music, str(mixed), volume_pct=music_volume, duration_s=duration
                )
            )
            voiced = mixed
        thumb = tmp / THUMB_NAME
        ffmpeg.run(ffmpeg.build_thumbnail_cmd(str(voiced), str(thumb), at_s=0.5))
        # On disk before the reel is marked ready: when the Mac froze right
        # after a render, the renamed-in files came back empty.
        for f in (voiced, thumb):
            with open(f, "rb") as fh:
                os.fsync(fh.fileno())
        os.replace(voiced, out)
        os.replace(thumb, reel_dir / THUMB_NAME)
        return duration
    finally:
        for f in tmp.iterdir():
            f.unlink(missing_ok=True)
        tmp.rmdir()


def remove_unused(reel_dir: Path, keep: set[Path]) -> None:
    """Drop pictures and voice clips no scene uses any more."""
    for f in reel_dir.glob("*"):
        if f.is_file() and f.suffix in (".png", ".wav") and f not in keep:
            f.unlink(missing_ok=True)
