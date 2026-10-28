"""Translate & dub a reel (Hindi).

The clip's transcript is split into sentences and translated in one call to
the local LLM (gemma3 via Ollama, the model "Create a reel" writes scripts
with). Each translated sentence is read out by Kokoro and placed where the
original sentence was said; one that would run into the next is read faster
(up to ``MAX_SPEEDUP``), and if it still doesn't fit the next one waits. The
clip's own audio is replaced by this voice; background music goes on top as
for any clip. Lip movements won't match: it is a voice-over.

The voice is read out once per dubbed clip (``dub.wav`` + ``dub.srt`` next to
``clip.mp4``) and laid over the picture again after every re-render, so a
colour change doesn't read it out again.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from app.services import ffmpeg, reel_script, tts
from app.services.captions import build_cue_srt
from app.services.reel_render import build_mux_cmd, write_wav
from app.services.render import srt_path_for
from app.services.transcriber import Word, _sentences_from_words

LANGUAGES = ("hi",)  # dub languages: keys of tts.LANGUAGES
MAX_SPEEDUP = 1.3  # a line longer than its time is read at most this much faster
_GAP_S = 0.05  # between two lines when one had to wait for the other
VOICE_NAME = "dub.wav"
SRT_NAME = "dub.srt"

# How each language should sound, on top of the general rules.
_STYLE = {
    "hi": "Everyday English words that Hindi speakers use as they are (phone, video, "
    "online, ...) may stay English, written in Devanagari.",
}


class DubError(RuntimeError):
    pass


def sentences(words: list[dict]) -> list[dict]:
    """The clip's words grouped into sentences, one dubbed line each:
    [{start_s, end_s, text}], seconds in the video."""
    grouped = _sentences_from_words([Word(w["word"], w["start"], w["end"]) for w in words])
    return [
        {"start_s": s["start"], "end_s": s["end"], "text": s["text"]} for s in grouped if s["text"]
    ]


def _messages(lines: list[dict], language: str) -> list[dict]:
    numbered = "\n".join(
        f"{n}. ({line['end_s'] - line['start_s']:.1f} s) {line['text']}"
        for n, line in enumerate(lines, start=1)
    )
    system = "You translate the speech of short videos for dubbing. Reply with JSON only."
    user = (
        f"Translate every numbered line into {tts.LANGUAGES[language].prompt_name}, the way "
        "people really talk in short social media videos: simple, natural and friendly, "
        f"not formal or bookish. {_STYLE.get(language, '')}\n"
        "Each line is read aloud in the time shown, so keep it about as short as the "
        "original; shorter is better. Translate only what is said: keep names (of people, "
        "shows, brands) and numbers, and never add placeholders, brackets, notes or "
        "explanations. Give exactly one translation per line, in the same order: never "
        "merge, split or skip lines.\n\n"
        f"{numbered}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# "{बिज़नेस नाम}", "[music]": placeholders and notes small models sometimes add.
_PLACEHOLDER = re.compile(r"\s*[{\[][^{}\[\]]*[}\]]\s*")
# "इनGuys": a Devanagari and an English word written as one.
_GLUED = re.compile(r"(?<=[ऀ-ॿ])(?=[A-Za-z])|(?<=[A-Za-z])(?=[ऀ-ॿ])")


def _clean(text: str) -> str:
    text = _GLUED.sub(" ", text)
    return " ".join(_PLACEHOLDER.sub(" ", text).split()) or " ".join(text.split())


def translate(lines: list[dict], language: str) -> list[str]:
    """One translation per line (one LLM call, ~5-20 s for a reel). Raises DubError."""
    schema = {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": len(lines),
                "maxItems": len(lines),
            }
        },
        "required": ["lines"],
    }
    num_predict = min(4000, 300 + 150 * len(lines))  # Devanagari takes many tokens
    for _attempt in range(2):  # small models now and then merge two lines: ask again
        try:
            raw = reel_script._chat(
                _messages(lines, language), schema, temperature=0.3, num_predict=num_predict
            )
        except reel_script.ScriptError as exc:
            raise DubError(str(exc)) from None
        out = raw.get("lines") if isinstance(raw, dict) else None
        if (
            isinstance(out, list)
            and len(out) == len(lines)
            and all(isinstance(t, str) and t.strip() for t in out)
        ):
            return [_clean(t) for t in out]
    raise DubError("The translation came back incomplete. Try again.")


def voice_track(lines: list[dict], language: str, voice: str, clip_start: float, duration: float):
    """Read every line out and place it where the original was said. Returns
    (samples, sample_rate, cues): mono float32 as long as the clip, and the
    (start, end, text) each line really got, in seconds from the clip start."""
    import numpy as np

    starts = [max(0.0, line["start_s"] - clip_start) for line in lines]
    placed: list[tuple[float, object, str]] = []
    sample_rate = None
    cursor = 0.0  # where the previous line ended
    for i, line in enumerate(lines):
        start = max(starts[i], cursor)
        slot = (starts[i + 1] if i + 1 < len(lines) else duration) - start
        samples, sample_rate = tts.synthesize(line["text"], language, voice)
        spoken = len(samples) / sample_rate
        if spoken > slot:
            speed = min(MAX_SPEEDUP, spoken / slot) if slot > 0 else MAX_SPEEDUP
            samples, sample_rate = tts.synthesize(line["text"], language, voice, speed=speed)
        placed.append((start, samples, line["text"]))
        cursor = start + len(samples) / sample_rate + _GAP_S
    if sample_rate is None:
        raise DubError("There is nothing to read out.")

    track = np.zeros(int(round(duration * sample_rate)), dtype=np.float32)
    cues = []
    for start, samples, text in placed:
        at = int(round(start * sample_rate))
        if at >= len(track):
            break  # pushed past the end of the reel
        piece = samples[: len(track) - at]
        track[at : at + len(piece)] += piece
        cues.append((start, start + len(piece) / sample_rate, text))
    return track, sample_rate, cues


def apply(dub: dict, clip_path: str, clip_start: float, duration: float) -> None:
    """Replace a freshly rendered clip's audio with the dubbed voice and write
    its captions. Call holding ``bgaudio.clip_lock``, before music is mixed in."""
    folder = Path(clip_path).parent
    voice, srt = folder / VOICE_NAME, folder / SRT_NAME
    if not voice.exists():
        samples, sample_rate, cues = voice_track(
            dub["lines"], dub["language"], dub["voice"], clip_start, duration
        )
        srt.write_text(build_cue_srt(cues), encoding="utf-8")
        write_wav(voice, samples, sample_rate)  # last: it marks both as complete
    tmp = folder / f"dub.part-{uuid.uuid4().hex}.mp4"
    try:
        ffmpeg.run(build_mux_cmd(clip_path, str(voice), str(tmp), duration))
        os.replace(tmp, clip_path)
    finally:
        tmp.unlink(missing_ok=True)
    shutil.copyfile(srt, srt_path_for(clip_path))
