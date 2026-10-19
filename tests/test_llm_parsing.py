from __future__ import annotations

import pytest

from app.config.platforms import get_preset
from app.services.cost import CostTracker
from app.services.llm import LLMClient, LLMResponse, parse_json_object
from app.services.scoring import HighlightScore, _parse_scores, score_candidates
from app.services.segments import Candidate


def test_parse_plain_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_parse_json_with_code_fence():
    raw = '```json\n{"a": 1, "b": [2,3]}\n```'
    assert parse_json_object(raw) == {"a": 1, "b": [2, 3]}


def test_parse_json_with_surrounding_prose():
    raw = 'Here is the result: {"score": 88} — hope it helps!'
    assert parse_json_object(raw) == {"score": 88}


def test_parse_json_raises_when_absent():
    with pytest.raises(ValueError):
        parse_json_object("no json here")


def test_highlight_score_coerces_string_hashtags():
    s = HighlightScore(
        index=0, score=50, hook_strength=40, standalone=True, hashtags="ai, tech news"
    )
    assert s.hashtags == ["ai", "tech", "news"]


def test_parse_scores_skips_invalid_items():
    raw = (
        '{"results": ['
        '{"index": 0, "score": 90, "hook_strength": 80, "standalone": true},'
        '{"index": 1, "score": 999, "hook_strength": 10, "standalone": false}'  # score out of range
        "]}"
    )
    scores = _parse_scores(raw, 2)
    assert len(scores) == 1
    assert scores[0].index == 0


class _FakeLLM(LLMClient):
    def __init__(self, content: str):
        self.content = content
        self.calls = 0

    def complete_json(self, *, model, system, user, max_tokens=1024):
        self.calls += 1
        return LLMResponse(content=self.content, model=model, tokens_in=100_000, tokens_out=50_000)


def test_score_candidates_assigns_and_tracks_cost():
    preset = get_preset("youtube_shorts")
    cands = [Candidate(0, 30, "hello world"), Candidate(30, 60, "second clip")]
    llm = _FakeLLM(
        '{"results": ['
        '{"index": 0, "score": 95, "hook_strength": 90, "standalone": true,'
        ' "suggested_title": "A", "hashtags": ["x"]},'
        '{"index": 1, "score": 40, "hook_strength": 30, "standalone": false}'
        "]}"
    )
    cost = CostTracker(cap_usd=10.0)
    pairs = score_candidates(llm, cost, cands, preset, "gpt-4o-mini")
    assert len(pairs) == 2
    assert pairs[0][1].score == 95
    assert cost.tokens_in == 100_000 and cost.tokens_out == 50_000
    assert cost.total_usd > 0


def test_score_candidates_handles_missing_index():
    preset = get_preset("youtube_shorts")
    cands = [Candidate(0, 30, "a"), Candidate(30, 60, "b")]
    # Only index 0 returned; index 1 should default to score 0.
    llm = _FakeLLM(
        '{"results": [{"index": 0, "score": 70, "hook_strength": 60, "standalone": true}]}'
    )
    cost = CostTracker(cap_usd=10.0)
    pairs = score_candidates(llm, cost, cands, preset, "gpt-4o-mini")
    by_text = {c.text: hs for c, hs in pairs}
    assert by_text["a"].score == 70
    assert by_text["b"].score == 0


def test_language_rule_is_chosen_from_the_transcript_script():
    from app.services.scoring import (
        _HINGLISH_RULE,
        _METADATA_SYSTEM,
        _SAME_LANGUAGE_RULE,
        _SCORING_SYSTEM,
        language_rule,
    )

    assert language_rule("उससे वो लड़का नहीं चाहिए उसका इरादा कुछ और") == _HINGLISH_RULE
    assert language_rule("We need to talk about what happened last night") == _SAME_LANGUAGE_RULE
    assert language_rule("") == _SAME_LANGUAGE_RULE
    # Mostly English with one Hindi word stays English.
    assert language_rule("this is a long english sentence with one word: हाँ") == _SAME_LANGUAGE_RULE

    preset = get_preset("facebook_video")
    rule = language_rule("यह हिंदी है")
    scoring = _SCORING_SYSTEM.format(label=preset.label, style=preset.style, language_rule=rule)
    metadata = _METADATA_SYSTEM.format(label=preset.label, language_rule=rule)
    for prompt in (scoring, metadata):
        assert "HINGLISH" in prompt and "NEVER use Devanagari" in prompt
    # The JSON shape braces survived .format().
    assert '{"results": [{"index"' in scoring
    assert '{"title": str' in metadata
