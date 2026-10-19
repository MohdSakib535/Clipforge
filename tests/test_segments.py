from __future__ import annotations

from app.services.segments import (
    Candidate,
    build_candidates,
    build_coverage_segments,
    select_non_overlapping,
    snap_boundary,
)


def _sentences():
    # 6 sentences, 10s each => 0..60s
    return [
        {"text": f"Sentence {i}.", "start": float(i * 10), "end": float(i * 10 + 10)}
        for i in range(6)
    ]


def test_candidates_respect_length_window():
    cands = build_candidates(_sentences(), scene_cuts=[], min_s=20, max_s=30)
    assert cands, "expected some candidates"
    for c in cands:
        assert 20 <= c.duration <= 30


def test_candidates_start_and_end_on_sentence_boundaries():
    sentences = _sentences()
    starts = {s["start"] for s in sentences}
    ends = {s["end"] for s in sentences}
    cands = build_candidates(sentences, scene_cuts=[], min_s=20, max_s=40)
    for c in cands:
        assert c.start in starts
        assert c.end in ends


def test_snap_boundary_moves_to_nearby_scene_cut():
    # scene cut at 10.3 is within tol of sentence end 10.0, no word spans it.
    words = [{"word": "a", "start": 9.0, "end": 9.8}]
    t = snap_boundary(10.0, scene_cuts=[10.3], words=words, tol=1.0)
    assert t == 10.3


def test_snap_boundary_refuses_mid_word_cut():
    # scene cut at 9.5 falls inside the word [9.0, 9.8] -> must not snap.
    words = [{"word": "hello", "start": 9.0, "end": 9.8}]
    t = snap_boundary(10.0, scene_cuts=[9.5], words=words, tol=1.0)
    assert t == 10.0


def test_snap_boundary_ignores_far_cuts():
    t = snap_boundary(10.0, scene_cuts=[25.0], words=[], tol=1.0)
    assert t == 10.0


def test_candidates_count_scenes_and_energy():
    cands = build_candidates(
        _sentences(),
        scene_cuts=[0.0, 15.0, 45.0],
        min_s=20,
        max_s=60,
        energy_peaks=[5.0, 25.0, 50.0],
    )
    assert cands
    first = cands[0]
    assert first.scene_count >= 1
    assert first.energy_peaks >= 1


def test_select_non_overlapping_picks_top_scores():
    a = Candidate(0, 10, "a")
    b = Candidate(5, 15, "b")  # overlaps a
    c = Candidate(20, 30, "c")
    chosen = select_non_overlapping([(90, a), (80, b), (70, c)], count=5)
    # b overlaps the higher-scored a and is dropped; a and c survive, time-sorted.
    assert [x.text for x in chosen] == ["a", "c"]


def test_select_respects_count():
    cands = [Candidate(i * 20, i * 20 + 10, str(i)) for i in range(5)]
    scored = [(float(i), c) for i, c in enumerate(cands)]
    chosen = select_non_overlapping(scored, count=2)
    assert len(chosen) == 2


def test_coverage_covers_whole_video_contiguously():
    # 60s of sentences (6 x 10s). Target 20s -> 3 clips covering 0..60 with no gaps.
    sentences = _sentences()
    covers = build_coverage_segments(sentences, scene_cuts=[], target_s=20)
    assert len(covers) == 3
    # Contiguous: each clip starts where the previous ended; whole video covered.
    assert covers[0].start == 0.0
    assert abs(covers[-1].end - 60.0) < 0.01
    for k in range(len(covers) - 1):
        assert covers[k + 1].start == covers[k].end  # no gaps, no overlap


def test_coverage_clip_count_scales_with_target():
    sentences = _sentences()  # 60s total
    short = build_coverage_segments(sentences, [], target_s=10)
    long = build_coverage_segments(sentences, [], target_s=30)
    assert len(short) > len(long)  # smaller target -> more clips
    # ~duration/target
    assert len(short) == 6
    assert len(long) == 2


def test_coverage_empty_transcript():
    assert build_coverage_segments([], [], target_s=20) == []
