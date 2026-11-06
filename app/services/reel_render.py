"""Put a reel together: one picture per scene with a slow camera move, short
transitions between scenes, the scene's voice-over, captions burned in with the
spoken word lit up, background music optional.

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
# Each picture zooms (or is zoomed in this much and pans) over its scene.
ZOOM = 0.15
# Scenes blend into each other over this long; the kinds (FFmpeg xfade) take turns.
TRANSITION_S = 0.4
# (Not "zoomin": it dives into the middle of the picture, often a flat patch.)
TRANSITIONS = ("fade", "smoothleft", "smoothup", "fadeblack")
# A light grade so the pictures look less flat: a bit more contrast and colour,
# darker corners.
GRADE = "eq=contrast=1.05:saturation=1.12,vignette=angle=PI/5"
# A reel from a brief keeps its look's colours (more saturation went against
# muted, sepia looks) and is sharpened a little: its pictures are scaled up.
BRIEF_GRADE = "eq=contrast=1.05,unsharp=5:5:0.5,vignette=angle=PI/5"
HIGHLIGHT = "&H0000D7FF&"  # the word being spoken: warm yellow (ASS colours are BGR)
# "Story" scripts are told like a story, not cut like a reel: the narrator a
# little slower, a breath between scenes while the picture fades into the next
# (the next line starts once it is there), every picture drifting slowly
# closer and calm captions.
STORY_SPEED = 0.92
# ...which makes Kokoro read ~6% fewer words a second, not 8% (measured).
STORY_PACE = 0.94
STORY_GAP_S = 0.8
STORY_ZOOM = 0.08
# A reel from a brief holds each picture ~13 s: the six moves take turns as in
# a reel (zoom in, pan, zoom out...), slower and eased in and out, so it keeps
# moving without losing the calm.
VARIED_ZOOM = 0.12
VIDEO_NAME = "reel.mp4"
THUMB_NAME = "thumb.jpg"


def reel_dir(reel_id: str) -> Path:
    return get_storage().path_for(f"reels/{reel_id}")


def _key(*parts: object) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:12]


def scene_pictures(folder: Path, script: dict, settings: Settings, style: str) -> list[Path]:
    """Where each scene's picture is (or will be) for the current script: a
    new look for a character, or a new setting, draws its pictures again."""
    return [
        picture_path(
            folder,
            i,
            scene_prompt(sc, script),
            style,
            settings.reel_image_width,
            settings.reel_image_height,
            settings.reel_image_steps,
        )  # fmt: skip
        for i, sc in enumerate(script["scenes"])
    ]


def scene_prompt(scene: dict, script: dict) -> str:
    """What a scene's picture shows: its moment (image_prompt), everyone in it
    with their look from the script's characters (the same words in every
    picture, so they look the same), how many people, and the story's setting
    (its time and place). A script from before characters gives just the
    image_prompt."""
    looks = {c["name"]: c["look"] for c in script.get("characters") or []}
    names = [n for n in scene.get("characters") or [] if n in looks]
    setting = (script.get("setting") or "").strip().rstrip(".")
    if not names and not setting:
        return scene["image_prompt"]
    parts = [scene["image_prompt"].strip().rstrip(".")]
    if names:
        parts.append("In the picture: " + "; ".join(f"{n}, {looks[n]}" for n in names))
        if scene.get("crowd"):
            parts.append("Other people only in the background, dressed for the time and place")
        else:
            many = "person" if len(names) == 1 else "people"
            parts.append(
                f"Only {len(names)} {many} in the picture, nobody else: no statues, idols or "
                "paintings of people"
            )
    if setting:
        parts.append(f"Time and place: {setting}. Everything in the picture belongs there")
    return ". ".join(p.strip().rstrip(".") for p in parts) + "."


def picture_problems(
    counts: dict[str, int] | None, scene: dict, *, text: bool = False
) -> list[str]:
    """What looks wrong in a scene's picture, from the faces, hands and people
    Vision saw in it: more people than the scene has (an extra person, or a
    second face), or more hands than they can have; with ``text`` (a reel
    from a brief), letters it could read (the model draws them garbled). []
    if it looks right, or if nothing was seen to judge by."""
    if not counts:
        return []
    faces, hands, people = counts["faces"], counts["hands"], counts["people"]
    problems = []
    named = scene.get("characters") or []
    if named and not scene.get("crowd") and max(faces, people) > len(named):
        problems.append(f"{max(faces, people)} people or faces instead of {len(named)}")
    if hands > 2 * max(faces, people, len(named), 1):
        problems.append(f"{hands} hands for {max(faces, people, len(named), 1)} people")
    if text and counts.get("text"):
        problems.append("letters drawn in the picture")
    return problems


def warning_path(picture: Path) -> Path:
    """Beside a picture that still looked wrong after being drawn again: why."""
    return picture.with_name(picture.name + ".warn")


def reseed_path(picture: Path) -> Path:
    """Beside a picture asked for again ("New picture"): draw it with a new
    seed, not the reel's (which would draw the same picture)."""
    return picture.with_name(picture.name + ".reseed")


def reel_seed(reel_id: str) -> int:
    """The seed every picture of a reel from a brief starts from: the same
    noise for each makes its characters and look come out more alike."""
    return int(_key("seed", reel_id), 16) % 2**31


def picture_path(
    reel_dir: Path, index: int, prompt: str, style: str, width: int, height: int, steps: int
) -> Path:
    key = _key(prompt, style, width, height, steps)
    return reel_dir / f"scene{index + 1:02d}-{key}.png"


def voice_path(reel_dir: Path, index: int, text: str, language: str, voice: str) -> Path:
    return reel_dir / f"voice{index + 1:02d}-{_key(text, language, voice)}.wav"


@dataclass(frozen=True)
class Style:
    label: str  # shown in the UI
    prompt: str  # put before every picture prompt


# The look of a reel's pictures, picked per reel. The script model describes
# only what is in each picture; the style says how it is drawn.
STYLES: dict[str, Style] = {
    "cinematic": Style(
        "Cinematic",
        "Cinematic film still, dramatic lighting, rich vibrant colours, shallow depth of "
        "field, highly detailed",
    ),
    "realistic": Style(
        "Realistic photo",
        "Realistic high-resolution photograph, natural colours and light, sharp focus, "
        "true-to-life detail",
    ),
    "anime": Style(
        "Anime",
        "Anime style illustration, clean line art, cel shading, vibrant colours, detailed "
        "painted background",
    ),
    "3d": Style(
        "3D cartoon",
        "3D animated cartoon render, soft lighting, expressive characters, vibrant colours, "
        "highly detailed",
    ),
}


def style_prompt(style: str) -> str:
    """A style's words: a key of STYLES, or the words themselves (a reel from a
    brief has its own look)."""
    return STYLES[style].prompt if style in STYLES else style


def picture_prompt(prompt: str, style: str) -> str:
    # Without a style the pictures looked like plain stock photos. The model
    # drew a 3-panel collage for "a montage of quick shots": hence "single".
    suffix = (
        "Natural, correct anatomy. One single vertical 9:16 picture, not a collage, no text, "
        "no logos."
    )
    return f"{style_prompt(style)}. {prompt.rstrip('. ')}. {suffix}"


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


def speech_spans(samples, sample_rate: int, *, min_gap_s: float = 0.1) -> list[tuple[float, float]]:
    """(start, end) seconds of the stretches of a voice clip between the
    silences tts puts between its sentences: runs of exact zeros, which a
    voice itself never has for 0.1 s."""
    import numpy as np

    zero = np.concatenate([[False], np.asarray(samples) == 0, [False]]).astype(np.int8)
    edges = np.diff(zero)
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    spans, pos = [], 0
    for a, b in zip(starts, ends, strict=True):
        if b - a >= min_gap_s * sample_rate:
            if a > pos:
                spans.append((pos / sample_rate, a / sample_rate))
            pos = b
    if pos < len(samples):
        spans.append((pos / sample_rate, len(samples) / sample_rate))
    return spans


def timed_cues(text: str, samples, sample_rate: int, **chunking) -> list[Cue]:
    """caption_cues timed to where each sentence is really spoken: a scene is
    read sentence by sentence with silence between (tts), so each sentence's
    chunks share only its own stretch of voice; timed over the whole scene,
    the lit-up word drifted by every pause. A caption stays up through the
    pause after it. As caption_cues if the stretches don't match the sentences."""
    from app.services.tts import sentences

    pieces = sentences(text)
    spans = speech_spans(samples, sample_rate)
    if len(pieces) < 2 or len(pieces) != len(spans):
        return caption_cues(text, len(samples) / sample_rate, **chunking)
    cues: list[Cue] = []
    for k, (piece, (start, end)) in enumerate(zip(pieces, spans, strict=True)):
        part = [
            Cue(c.start + start, c.end + start, c.text)
            for c in caption_cues(piece, end - start, **chunking)
        ]
        if part and k + 1 < len(spans):
            last = part[-1]
            part[-1] = Cue(last.start, spans[k + 1][0], last.text)
        cues += part
    return cues


def _ass_time(t: float) -> str:
    cs = round(max(0.0, t) * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(cues: list[Cue], font: str, *, story: bool = False) -> str:
    """White bold text with a thick outline, a "pop" as each chunk appears and
    the word being spoken in yellow (its time is guessed from its length, as
    for the chunks). It sits at ~70% height: above the caption/username area
    the apps draw at the bottom, below the middle of the picture."""
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
        f"Style: Caption,{font},{80 if story else 92},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,8,3,2,80,80,560,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    pop = "{\\fscx80\\fscy80\\t(0,80,\\fscx108\\fscy108)\\t(80,150,\\fscx100\\fscy100)}"
    if story:
        pop = ""  # calm: no "pop" as each chunk appears
    for c in cues:
        words = c.text.replace("\\", "").replace("{", "(").replace("}", ")").split()
        if not words:
            continue
        weights = [len(w) + 1 for w in words]
        per_char = (c.end - c.start) / sum(weights)
        start = c.start
        for k, weight in enumerate(weights):
            end = c.end if k == len(words) - 1 else start + weight * per_char
            text = " ".join(
                f"{{\\c{HIGHLIGHT}}}{w}{{\\c&HFFFFFF&}}" if j == k else w
                for j, w in enumerate(words)
            )
            lines.append(
                f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,"
                f"{pop if k == 0 else ''}{text}"
            )
            start = end
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# FFmpeg commands (pure functions -> unit-testable)
# ---------------------------------------------------------------------------
def _filter_path(path: str) -> str:
    """Escape a file path for use inside a filtergraph option."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def camera_move(
    index: int, frames: int, *, story: bool = False, varied: bool = False
) -> tuple[str, str, str]:
    """zoompan's (zoom, x, y) for scene ``index``; the moves take turns: zoom
    in, pan right, zoom out, pan up, pan left, pan down. A story drifts
    slowly closer in every scene, so the scenes flow into one another;
    ``varied`` (a reel from a brief): the moves take turns, VARIED_ZOOM far,
    starting and ending gently."""
    p = f"on/{frames}"
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if story and not varied:
        return (f"1+{STORY_ZOOM}*{p}", cx, cy)
    amount = ZOOM
    if varied:
        amount = VARIED_ZOOM
        p = f"(1-cos(PI*on/{frames}))/2"  # eased: 0 to 1, slow at both ends
    zoomed = f"{1 + amount}"
    moves = [
        (f"1+{amount}*{p}", cx, cy),
        (zoomed, f"(iw-iw/zoom)*{p}", cy),
        (f"{1 + amount}-{amount}*{p}", cx, cy),
        (zoomed, cx, f"(ih-ih/zoom)*(1-{p})"),
        (zoomed, f"(iw-iw/zoom)*(1-{p})", cy),
        (zoomed, cx, f"(ih-ih/zoom)*{p}"),
    ]
    return moves[index % len(moves)]


def _encode(preset: str) -> list[str]:
    """How every piece of a reel is encoded: the same everywhere, so the
    pieces join without being encoded again."""
    return ["-c:v", "libx264", "-preset", preset, "-crf", "20", "-pix_fmt", "yuv420p", "-an"]


def build_scene_cmd(
    picture: str,
    dst: str,
    *,
    frames: int,
    move: int,
    ass: str | None,
    preset: str,
    splits: list[int] | None = None,
    story: bool = False,
    varied: bool = False,
    grade: str = GRADE,
) -> list[str]:
    """One scene: the picture scaled to cover the frame (at twice the output
    size, so the move doesn't wobble), moved slowly, graded, captions on top.
    Video only; the voice is added to the whole reel at once. ``splits``: the
    frames where it is cut into pieces (``dst`` then has a "%d": one file per
    piece), each starting on a keyframe so the pieces join without encoding."""
    z, x, y = camera_move(move, frames, story=story, varied=varied)
    chain = (
        f"scale={2 * WIDTH}:{2 * HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={2 * WIDTH}:{2 * HEIGHT},"
        f"zoompan=z={z}:x={x}:y={y}:d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},{grade}"
    )
    if ass:
        chain += f",ass={_filter_path(ass)}"
    chain += ",format=yuv420p"
    cmd = ["ffmpeg", "-y", "-i", picture, "-vf", chain, "-frames:v", str(frames), *_encode(preset)]
    if splits:
        cmd += [
            "-forced-idr", "1",
            "-force_key_frames", "expr:" + "+".join(f"eq(n,{f})" for f in splits),
            "-f", "segment", "-segment_frames", ",".join(map(str, splits)),
            "-reset_timestamps", "1", "-segment_format", "mp4",
        ]  # fmt: skip
    return [*cmd, dst]


def build_blend_cmd(tail: str, head: str, dst: str, *, kind: str, frames: int, preset: str):
    """The blend from one scene into the next: the end of one (``tail``) into
    the start of the other (``head``), both ``frames`` long. Two small inputs:
    a long reel's blends once took ~3 GB of RAM in a single FFmpeg."""
    return [
        "ffmpeg", "-y",
        "-i", tail,
        "-i", head,
        "-filter_complex",
        f"[0:v][1:v]xfade=transition={kind}:duration={frames / FPS:.6f}:offset=0[v]",
        "-map", "[v]",
        "-frames:v", str(frames),
        *_encode(preset),
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
def scene_timing(
    frames: list[int], spoken: list[int], *, story: bool = False
) -> tuple[list[int], list[int], list[int]]:
    """How the scenes' pictures line up with their voice: (frames of picture
    per scene, frames of each blend into the next, frames into its picture at
    which each scene's line starts).

    ``frames``: each scene's slot (its line plus the breath after it),
    ``spoken``: the frames of the line itself. A reel blends into the next
    picture as that scene's line starts (0.4 s). A story blends during the
    breath before it (0.8 s): the next line starts once its picture is there,
    so each picture starts earlier than its slot. Every scene keeps at least
    one frame of its own."""
    count = len(frames)
    if not story:
        full = round(TRANSITION_S * FPS)
        blends = [max(1, min(full, frames[i + 1] - 1)) for i in range(count - 1)]
        return list(frames), blends, [0] * count
    full = round(STORY_GAP_S * FPS)
    blends = [
        max(1, min(full, frames[i] - spoken[i], frames[i] - 1, frames[i + 1] - 1))
        for i in range(count - 1)
    ]
    starts = [0] + blends  # the blend into a scene runs before its line starts
    shown = [frames[i] - (blends[i] if i < count - 1 else 0) + starts[i] for i in range(count)]
    return shown, blends, starts


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
    story: bool = False,
    varied: bool = False,
    grade: str = GRADE,
) -> float:
    """Write ``reel.mp4`` and ``thumb.jpg`` in ``reel_dir``; returns the length
    in seconds. ``story``: told like a story (see STORY_SPEED); ``varied``:
    the camera moves take turns (see VARIED_ZOOM). Raises FFmpegError."""
    import numpy as np

    tmp = reel_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        # Each scene lasts its line plus a breath, rounded up to whole frames so
        # picture and voice stay in step over the whole reel.
        tracks, frames, speech, clips, sample_rate = [], [], [], [], None
        for i, sc in enumerate(scenes):
            samples, sr = read_wav(sc.voice)
            if sample_rate not in (None, sr):
                raise ffmpeg.FFmpegError("voice clips have different sample rates")
            sample_rate = sr
            if story:  # soft edges: the joins between the lines can't be heard
                samples = _soften(samples, sr)
            speech.append(len(samples) / sr)
            clips.append(samples)
            gap = END_GAP_S if i == len(scenes) - 1 else (STORY_GAP_S if story else GAP_S)
            n = math.ceil((speech[i] + gap) * FPS)
            pad = round(n / FPS * sr) - len(samples)
            tracks.append(np.concatenate([samples, np.zeros(max(0, pad), np.float32)]))
            frames.append(n)

        # Scene i blends into scene i+1 over blends[i] frames, at the start of
        # scene i+1's picture: scene i runs that much longer (its "tail"),
        # scene i+1's first frames (its "head") go into the blend, and the
        # rest of each (its "body") is used as it is.
        count = len(scenes)
        spoken = [math.ceil(s * FPS) for s in speech]
        shown, blends, starts = scene_timing(frames, spoken, story=story)

        def _scene(i: int) -> dict[str, Path]:
            """The scene, encoded once and cut into its pieces: {"head", "body", "tail"}."""
            sc = scenes[i]
            ass = tmp / f"captions{i + 1:02d}.ass"
            longer = {"max_words": 5, "max_chars": 30} if story else {}  # calm: a line, not words
            cues = timed_cues(sc.text, clips[i], sample_rate, **longer)
            late = starts[i] / FPS  # its line starts this far into its picture
            cues = [Cue(c.start + late, c.end + late, c.text) for c in cues]
            ass.write_text(build_ass(cues, font, story=story), encoding="utf-8")
            head = blends[i - 1] if i > 0 else 0
            tail = blends[i] if i < count - 1 else 0
            names = ["head"] * bool(head) + ["body"] + ["tail"] * bool(tail)
            splits = [f for f in (head, shown[i]) if 0 < f < shown[i] + tail]
            stem = f"scene{i + 1:02d}"
            dst = tmp / (f"{stem}-%d.mp4" if splits else f"{stem}.mp4")
            ffmpeg.run(
                build_scene_cmd(
                    str(sc.picture), str(dst), frames=shown[i] + tail, move=i, ass=str(ass),
                    preset=preset, splits=splits, story=story, varied=varied, grade=grade,
                )
            )  # fmt: skip
            if not splits:
                return {"body": dst}
            pieces = {name: tmp / f"{stem}-{k}.mp4" for k, name in enumerate(names)}
            if not all(p.exists() for p in pieces.values()):
                raise ffmpeg.FFmpegError(f"scene {i + 1} could not be cut into its pieces")
            return pieces

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            pieces = list(pool.map(_scene, range(count)))

            def _blend(i: int) -> Path:
                dst = tmp / f"blend{i + 1:02d}.mp4"
                ffmpeg.run(
                    build_blend_cmd(
                        str(pieces[i]["tail"]), str(pieces[i + 1]["head"]), str(dst),
                        kind="fade" if story else TRANSITIONS[i % len(TRANSITIONS)],
                        frames=blends[i], preset=preset,
                    )
                )  # fmt: skip
                return dst

            blended = list(pool.map(_blend, range(count - 1)))

        order = []  # body 1, blend 1-2, body 2, blend 2-3, ..., last body
        for i in range(count):
            order.append(pieces[i]["body"])
            if i < count - 1:
                order.append(blended[i])
        list_file = tmp / "pieces.txt"
        list_file.write_text("".join(f"file '{p.name}'\n" for p in order))
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
                    str(voiced),
                    music,
                    str(mixed),
                    volume_pct=music_volume,
                    duration_s=duration,
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


def _soften(samples, sample_rate: int):
    """A line with a 10 ms fade in and a 40 ms fade out."""
    import numpy as np

    out = np.array(samples, dtype=np.float32, copy=True)
    fade_in = min(int(0.01 * sample_rate), len(out) // 2)
    fade_out = min(int(0.04 * sample_rate), len(out) // 2)
    if fade_in:
        out[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    if fade_out:
        out[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    return out


def remove_unused(reel_dir: Path, keep: set[Path]) -> None:
    """Drop pictures and voice clips no scene uses any more (with the
    warnings and "draw again" marks beside the pictures)."""
    beside = (".warn", ".reseed")
    for f in reel_dir.glob("*"):
        used = f.with_suffix("") if f.suffix in beside else f
        if f.is_file() and f.suffix in (".png", ".wav", *beside) and used not in keep:
            f.unlink(missing_ok=True)
