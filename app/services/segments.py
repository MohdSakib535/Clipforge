from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Candidate:
    """One part of the video that becomes a clip: its time range and transcript."""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def _in_word(t: float, words: list[dict]) -> bool:
    """True if ``t`` falls strictly inside a spoken word (would cut mid-word)."""
    for w in words:
        if w["start"] < t < w["end"]:
            return True
    return False


def snap_boundary(t: float, scene_cuts: list[float], words: list[dict], tol: float = 1.0) -> float:
    """Snap ``t`` to the nearest scene cut within ``tol`` seconds, unless that
    cut would land mid-word. Otherwise return ``t`` unchanged."""
    if not scene_cuts:
        return t
    best = min(scene_cuts, key=lambda c: abs(c - t))
    if abs(best - t) <= tol and not _in_word(best, words):
        return best
    return t


def build_coverage_segments(
    sentences: list[dict],
    scene_cuts: list[float],
    target_s: float,
    *,
    duration_s: float | None = None,
    words: list[dict] | None = None,
    snap_tol: float = 1.0,
) -> list[Candidate]:
    """Tile the WHOLE video, 0 to ``duration_s``, into contiguous ~``target_s`` clips.

    The output clips reconstruct the full video: no gaps, no overlap.
    Parts with no speech (intros, music, nature footage) are covered too — they
    are cut on time. A cut never falls inside a sentence: if the target time
    lands mid-sentence the clip runs on to the end of that sentence. A
    remainder shorter than a quarter of ``target_s`` is folded into the last
    clip. Without ``duration_s`` the video is taken to end with the last
    sentence.
    """
    words = words or []
    scene_cuts = sorted(scene_cuts or [])
    video_end = duration_s or (sentences[-1]["end"] if sentences else 0.0)
    if video_end <= 0 or target_s <= 0:
        return []
    min_tail = target_s * 0.25

    out: list[Candidate] = []
    start = 0.0
    while video_end - start > 1e-6:
        cut = _next_safe_cut(start + target_s, sentences, scene_cuts, words, snap_tol)
        if cut <= start:  # pathological snap; always make progress
            cut = start + target_s
        if cut >= video_end or video_end - cut < min_tail:
            cut = video_end
        idx = [i for i, s in enumerate(sentences) if start - 1e-6 <= s["start"] < cut]
        out.append(
            Candidate(
                start=round(start, 3),
                end=round(cut, 3),
                text=" ".join(sentences[i]["text"] for i in idx).strip(),
            )
        )
        start = cut
    return out


def _next_safe_cut(
    t: float,
    sentences: list[dict],
    scene_cuts: list[float],
    words: list[dict],
    snap_tol: float,
) -> float:
    """First point at or after ``t`` where a clip may end: the end of the
    sentence ``t`` falls inside, or ``t`` itself (snapped to a nearby scene cut)
    when it falls in a pause or a stretch with no speech."""
    for s in sentences:
        if s["start"] < t < s["end"]:
            return s["end"]
    return snap_boundary(t, scene_cuts, words, snap_tol)
