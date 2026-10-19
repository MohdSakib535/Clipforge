from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config.platforms import PlatformPreset
from app.core.logging import get_logger
from app.services.cost import CostTracker
from app.services.llm import LLMClient, parse_json_object
from app.services.segments import Candidate

log = get_logger(__name__)


class HighlightScore(BaseModel):
    index: int
    score: float = Field(ge=0, le=100)
    hook_strength: float = Field(ge=0, le=100)
    standalone: bool
    reason: str = ""
    suggested_title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("hashtags", mode="before")
    @classmethod
    def _coerce_hashtags(cls, v):
        if isinstance(v, str):
            return [h.strip() for h in v.replace(",", " ").split() if h.strip()]
        return v


_HINGLISH_RULE = (
    " LANGUAGE: the transcript is Hindi. Write suggested_title/title, description and "
    "hashtags in HINGLISH: Hindi words spelled in Roman (English) letters, the way "
    'Indian audiences type on social media, e.g. "Kya aap yeh raaz jaante hain?". '
    "NEVER use Devanagari characters in those fields. Do not invent character names "
    "that are not clearly spoken in the transcript."
)
_SAME_LANGUAGE_RULE = (
    " LANGUAGE: write suggested_title/title, description and hashtags in the same "
    "language as the transcript. Do not invent character names that are not clearly "
    "spoken in the transcript."
)


def language_rule(text: str) -> str:
    """Prompt rule for the audience-facing text ('reason' stays English: it is
    for the person reading the dashboard). Decided in code, not by the LLM —
    given a Devanagari transcript the model copies the script no matter how the
    prompt asks otherwise."""
    letters = [ch for ch in text if ch.isalpha()]
    devanagari = sum(1 for ch in letters if "\u0900" <= ch <= "\u097f")
    if letters and devanagari / len(letters) > 0.3:
        return _HINGLISH_RULE
    return _SAME_LANGUAGE_RULE


_SCORING_SYSTEM = (
    "You are a video editor who selects viral-worthy clips. You are given "
    "candidate segments from a long video transcript. Score each as a standalone "
    "clip for the target platform. Respond ONLY with a JSON object of the form "
    '{{"results": [{{"index": int, "score": 0-100, "hook_strength": 0-100, '
    '"standalone": bool, "reason": str, "suggested_title": str, "description": '
    'str, "hashtags": [str]}}]}}. One result per candidate index. '
    "Target platform: {label}. Desired style: {style}. "
    "Score higher when the segment has a strong hook early, is self-contained, "
    "has a clear payoff, and fits the platform. hashtags: 3-6 relevant tags "
    "without the # symbol.{language_rule}"
)


def _build_user_prompt(candidates: list[Candidate]) -> str:
    items = []
    for i, c in enumerate(candidates):
        items.append(
            {
                "index": i,
                "duration_s": round(c.duration, 1),
                "scene_changes": c.scene_count,
                "energy_peaks": c.energy_peaks,
                "transcript": c.text[:1800],
            }
        )
    return json.dumps({"candidates": items}, ensure_ascii=False)


def _parse_scores(raw: str, batch_len: int) -> list[HighlightScore]:
    obj = parse_json_object(raw)
    results = obj.get("results", obj if isinstance(obj, list) else [])
    scores: list[HighlightScore] = []
    for item in results:
        try:
            scores.append(HighlightScore(**item))
        except (ValidationError, TypeError) as exc:
            log.warning("bad_score_item", extra={"error": str(exc)})
    return scores


def _score_one_batch(
    client: LLMClient, system: str, batch: list[Candidate], model: str
) -> tuple[dict[int, HighlightScore], int, int]:
    """Score a single batch. Returns (scores_by_local_index, tokens_in, tokens_out)."""
    user = _build_user_prompt(batch)
    tokens_in = tokens_out = 0
    scores_by_index: dict[int, HighlightScore] = {}
    # One retry on unparsable / empty JSON (API-level retries live in the client).
    for attempt in range(2):
        resp = client.complete_json(model=model, system=system, user=user, max_tokens=1600)
        tokens_in += resp.tokens_in
        tokens_out += resp.tokens_out
        parsed = _parse_scores(resp.content, len(batch))
        scores_by_index = {s.index: s for s in parsed}
        if scores_by_index:
            break
        log.warning("score_retry", extra={"attempt": attempt})
    return scores_by_index, tokens_in, tokens_out


def score_candidates(
    client: LLMClient,
    cost: CostTracker,
    candidates: list[Candidate],
    preset: PlatformPreset,
    model: str,
    *,
    batch_size: int = 8,
    concurrency: int = 4,
) -> list[tuple[Candidate, HighlightScore]]:
    """Score every candidate for a platform. Returns (candidate, score) pairs.

    Batches run concurrently (network I/O bound). Cost is applied after each
    batch returns, so the per-job cap may be enforced slightly late under
    parallelism; scoring cost is small, so this is an acceptable trade for
    much faster wall-clock on long videos.
    """
    sample = " ".join(c.text[:300] for c in candidates[:20])
    system = _SCORING_SYSTEM.format(
        label=preset.label, style=preset.style, language_rule=language_rule(sample)
    )
    batches = [candidates[i : i + batch_size] for i in range(0, len(candidates), batch_size)]
    workers = max(1, min(concurrency, len(batches)))

    def _work(batch: list[Candidate]):
        return batch, _score_one_batch(client, system, batch, model)

    results: list[tuple[list[Candidate], tuple[dict, int, int]]] = []
    if workers > 1 and len(batches) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_work, batches))
    else:
        results = [_work(b) for b in batches]

    out: list[tuple[Candidate, HighlightScore]] = []
    for batch, (scores_by_index, tin, tout) in results:
        cost.add_chat(model, tin, tout)  # may raise CostCapExceeded
        for local_idx, cand in enumerate(batch):
            score = scores_by_index.get(local_idx)
            if score is None:
                score = HighlightScore(index=local_idx, score=0, hook_strength=0, standalone=False)
            out.append((cand, score))
    return out


_METADATA_SYSTEM = (
    "You write platform-optimised titles, descriptions and hashtags for short "
    "video clips. Respond ONLY with JSON: "
    '{{"title": str, "description": str, "hashtags": [str]}}. '
    "Platform: {label}. Keep the title under 100 characters, punchy and "
    "click-worthy. 3-6 hashtags without the # symbol.{language_rule}"
)


class ClipMetadata(BaseModel):
    title: str = ""
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)

    @field_validator("hashtags", mode="before")
    @classmethod
    def _coerce(cls, v):
        if isinstance(v, str):
            return [h.strip() for h in v.replace(",", " ").split() if h.strip()]
        return v


def refine_metadata(
    client: LLMClient,
    cost: CostTracker,
    transcript: str,
    preset: PlatformPreset,
    model: str,
) -> ClipMetadata:
    """Generate stronger final metadata for a selected clip."""
    system = _METADATA_SYSTEM.format(
        label=preset.label, language_rule=language_rule(transcript[:600])
    )
    user = json.dumps({"transcript": transcript[:3000]}, ensure_ascii=False)
    resp = client.complete_json(model=model, system=system, user=user, max_tokens=400)
    cost.add_chat(model, resp.tokens_in, resp.tokens_out)
    try:
        return ClipMetadata(**parse_json_object(resp.content))
    except (ValidationError, ValueError) as exc:
        log.warning("bad_metadata", extra={"error": str(exc)})
        return ClipMetadata()
