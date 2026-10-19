from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    start: float
    end: float
    text: str
    sentence_indices: list[int] = field(default_factory=list)
    scene_count: int = 0
    energy_peaks: int = 0

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


def _count_in_range(points: list[float], start: float, end: float) -> int:
    return sum(1 for p in points if start <= p <= end)


def build_candidates(
    sentences: list[dict],
    scene_cuts: list[float],
    min_s: float,
    max_s: float,
    *,
    words: list[dict] | None = None,
    energy_peaks: list[float] | None = None,
    snap_tol: float = 1.0,
    max_candidates: int = 150,
) -> list[Candidate]:
    """Build candidate clip windows from sentence boundaries.

    Windows respect the preset length range [min_s, max_s]. Boundaries start on
    sentence starts (snapped to nearby scene cuts) and end on sentence ends, so
    a clip is never cut mid-word. Overlapping candidates are expected; scoring
    and selection narrow them down later.
    """
    words = words or []
    scene_cuts = sorted(scene_cuts or [])
    energy_peaks = energy_peaks or []
    cands: list[Candidate] = []
    n = len(sentences)

    for i in range(n):
        raw_start = sentences[i]["start"]
        start = snap_boundary(raw_start, scene_cuts, words, snap_tol)
        texts: list[str] = []
        for j in range(i, n):
            end_raw = sentences[j]["end"]
            texts.append(sentences[j]["text"])
            if end_raw - start < min_s:
                continue
            if end_raw - start > max_s:
                break
            end = snap_boundary(end_raw, scene_cuts, words, snap_tol)
            dur = end - start
            if min_s <= dur <= max_s:
                cands.append(
                    Candidate(
                        start=round(start, 3),
                        end=round(end, 3),
                        text=" ".join(texts).strip(),
                        sentence_indices=list(range(i, j + 1)),
                        scene_count=_count_in_range(scene_cuts, start, end),
                        energy_peaks=_count_in_range(energy_peaks, start, end),
                    )
                )
        if len(cands) >= max_candidates:
            break

    return _dedupe(cands)[:max_candidates]


def build_coverage_segments(
    sentences: list[dict],
    scene_cuts: list[float],
    target_s: float,
    *,
    words: list[dict] | None = None,
    energy_peaks: list[float] | None = None,
    snap_tol: float = 1.0,
) -> list[Candidate]:
    """Tile the WHOLE transcript into contiguous ~``target_s`` clips.

    Unlike :func:`build_candidates` (which produces many overlapping windows for
    the scorer to pick from), this covers the entire video start-to-end with no
    gaps and no overlap, so the output clips reconstruct the full video. Each
    segment grows by whole sentences until it reaches ``target_s``, then the next
    segment starts where it ended. Boundaries snap to scene cuts and never fall
    mid-word. The final segment may be shorter than ``target_s``.
    """
    if not sentences:
        return []
    words = words or []
    scene_cuts = sorted(scene_cuts or [])
    energy_peaks = energy_peaks or []
    out: list[Candidate] = []
    n = len(sentences)

    i = 0
    while i < n:
        start = snap_boundary(sentences[i]["start"], scene_cuts, words, snap_tol)
        texts: list[str] = []
        j = i
        while j < n:
            texts.append(sentences[j]["text"])
            if sentences[j]["end"] - start >= target_s:
                break
            j += 1
        last = min(j, n - 1)
        end = snap_boundary(sentences[last]["end"], scene_cuts, words, snap_tol)
        if end <= start:  # pathological; force progress
            end = sentences[last]["end"]
        out.append(
            Candidate(
                start=round(start, 3),
                end=round(end, 3),
                text=" ".join(texts).strip(),
                sentence_indices=list(range(i, last + 1)),
                scene_count=_count_in_range(scene_cuts, start, end),
                energy_peaks=_count_in_range(energy_peaks, start, end),
            )
        )
        i = last + 1
    return out


def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    seen: set[tuple[int, int]] = set()
    out: list[Candidate] = []
    for c in cands:
        key = (round(c.start), round(c.end))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def select_non_overlapping(
    scored: list[tuple[float, Candidate]], count: int, *, min_gap_s: float = 0.0
) -> list[Candidate]:
    """Greedily pick the top-``count`` candidates by score that do not overlap.

    ``scored`` is a list of (score, candidate). Higher score wins ties for a
    time region."""
    chosen: list[Candidate] = []
    for _, cand in sorted(scored, key=lambda x: x[0], reverse=True):
        if len(chosen) >= count:
            break
        if any(_overlaps(cand, c, min_gap_s) for c in chosen):
            continue
        chosen.append(cand)
    chosen.sort(key=lambda c: c.start)
    return chosen


def _overlaps(a: Candidate, b: Candidate, gap: float) -> bool:
    return a.start < b.end + gap and b.start < a.end + gap
