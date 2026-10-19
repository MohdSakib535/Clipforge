from __future__ import annotations

from dataclasses import dataclass

_MAX_WORDS_PER_LINE = 6
_MAX_LINE_DURATION = 2.5


@dataclass
class _Line:
    words: list[dict]

    @property
    def start(self) -> float:
        return self.words[0]["start"]

    @property
    def end(self) -> float:
        return self.words[-1]["end"]


def _group_lines(words: list[dict]) -> list[_Line]:
    lines: list[_Line] = []
    buf: list[dict] = []
    for w in words:
        buf.append(w)
        text = w["word"].strip()
        too_long = len(buf) >= _MAX_WORDS_PER_LINE
        too_slow = (w["end"] - buf[0]["start"]) >= _MAX_LINE_DURATION
        sentence_end = text.endswith((".", "!", "?", "…", ","))
        if too_long or too_slow or sentence_end:
            lines.append(_Line(buf))
            buf = []
    if buf:
        lines.append(_Line(buf))
    return lines


def _fmt_srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words: list[dict]) -> str:
    """SubRip captions (one cue per caption line) for platforms that accept an
    uploaded caption file. ``words`` have times relative to the clip start."""
    cues = []
    for n, line in enumerate(_group_lines(words), start=1):
        text = " ".join(w["word"].strip() for w in line.words)
        cues.append(f"{n}\n{_fmt_srt_time(line.start)} --> {_fmt_srt_time(line.end)}\n{text}\n")
    return "\n".join(cues)


def write_srt(path: str, words: list[dict]) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_srt(words))
    return path
