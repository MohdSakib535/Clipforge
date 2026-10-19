from __future__ import annotations

from app.models import Job
from app.services.scoring import HighlightScore
from app.services.segments import Candidate
from app.tasks.pipeline import _clip_count_for, _coverage_score, _with_fallback


def test_coverage_keeps_llm_score_and_fills_missing_text():
    fallback = _coverage_score(Candidate(0, 60, "Hello there everyone welcome back"), rank=3)
    scored = HighlightScore(
        index=0, score=82, hook_strength=70, standalone=True, hashtags=["movie"]
    )  # LLM returned a score but no title/description/reason
    merged = _with_fallback(scored, fallback)
    assert merged.score == 82 and merged.hashtags == ["movie"]
    assert merged.suggested_title.startswith("Part 3 - Hello there")
    assert merged.description == "Hello there everyone welcome back"


def test_coverage_prefers_llm_title():
    fallback = _coverage_score(Candidate(0, 60, "some text"), rank=1)
    scored = HighlightScore(
        index=0, score=60, hook_strength=50, standalone=True, suggested_title="The Twist"
    )
    assert _with_fallback(scored, fallback).suggested_title == "The Twist"


def test_clip_count_per_platform_override():
    job = Job(
        platforms=["youtube_shorts", "facebook_reel"],
        settings={"clip_count": 5, "clip_counts": {"facebook_reel": 8}},
    )
    assert _clip_count_for(job, "facebook_reel") == 8
    # falls back to job-wide default
    assert _clip_count_for(job, "youtube_shorts") == 5


def test_clip_count_default_when_unset():
    job = Job(platforms=["youtube_shorts"], settings={})
    assert _clip_count_for(job, "youtube_shorts") == 5


def test_clip_count_floor_is_one():
    job = Job(platforms=["youtube_shorts"], settings={"clip_counts": {"youtube_shorts": 0}})
    assert _clip_count_for(job, "youtube_shorts") == 1
