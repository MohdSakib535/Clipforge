from __future__ import annotations

from app.services.segments import build_coverage_segments, snap_boundary


def _sentences():
    # 6 sentences, 10s each => 0..60s
    return [
        {"text": f"Sentence {i}.", "start": float(i * 10), "end": float(i * 10 + 10)}
        for i in range(6)
    ]


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


def _assert_tiles(covers, duration):
    assert covers[0].start == 0.0
    assert abs(covers[-1].end - duration) < 0.01
    for k in range(len(covers) - 1):
        assert covers[k + 1].start == covers[k].end  # no gaps, no overlap


def test_coverage_includes_leading_and_trailing_parts_without_speech():
    # Like a nature film: music only until 190s, speech 190-500s, music to 510s.
    sentences = [
        {"text": f"s{i}", "start": 190.0 + i * 10, "end": 198.0 + i * 10} for i in range(31)
    ]
    covers = build_coverage_segments(sentences, [], 180, duration_s=510)
    _assert_tiles(covers, 510)
    assert covers[0].text == ""  # the silent intro is a clip too
    assert len(covers) == 3


def test_coverage_with_no_speech_at_all_is_cut_on_time():
    covers = build_coverage_segments([], [], 30, duration_s=95)
    _assert_tiles(covers, 95)
    # 95 = 30+30+35: the 5s remainder is folded into the last clip.
    assert [round(c.duration) for c in covers] == [30, 30, 35]


def test_coverage_never_cuts_inside_a_sentence():
    sentences = [
        {"text": "a", "start": 0.0, "end": 9.0},
        {"text": "long one", "start": 25.0, "end": 41.0},  # target 30 lands inside
        {"text": "b", "start": 70.0, "end": 80.0},
    ]
    covers = build_coverage_segments(sentences, [], 30, duration_s=100)
    _assert_tiles(covers, 100)
    assert covers[0].end == 41.0
    assert covers[0].text == "a long one"
