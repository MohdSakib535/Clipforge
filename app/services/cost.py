from __future__ import annotations

from dataclasses import dataclass, field

# Approximate OpenAI transcription prices (USD per audio minute). Kept here so
# one edit updates every estimate.
TRANSCRIBE_USD_PER_MIN: dict[str, float] = {
    "whisper-1": 0.006,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}

_DEFAULT_TRANSCRIBE = 0.006


def transcription_cost(model: str, minutes: float) -> float:
    rate = TRANSCRIBE_USD_PER_MIN.get(model, _DEFAULT_TRANSCRIBE)
    return rate * minutes


class CostCapExceeded(RuntimeError):
    def __init__(self, spent: float, cap: float):
        super().__init__(
            f"Estimated cost ${spent:.2f} exceeded the per-job cap of ${cap:.2f}. "
            "Job stopped. Raise MAX_JOB_COST_USD, use a shorter video, or set "
            "TRANSCRIBER=local (free)."
        )
        self.spent = spent
        self.cap = cap


@dataclass
class CostTracker:
    """Accumulates per-job cost and enforces a hard cap."""

    cap_usd: float
    audio_minutes: float = 0.0
    models_used: dict[str, float] = field(default_factory=dict)
    _total: float = 0.0

    def add_transcription(self, model: str, minutes: float, *, free: bool = False) -> None:
        """``free``: a local model — the minutes are still counted, no dollars."""
        self.audio_minutes += minutes
        cost = 0.0 if free else transcription_cost(model, minutes)
        self._total += cost
        self.models_used[model] = self.models_used.get(model, 0.0) + cost
        self._enforce()

    @property
    def total_usd(self) -> float:
        return round(self._total, 4)

    def _enforce(self) -> None:
        if self._total > self.cap_usd:
            raise CostCapExceeded(self._total, self.cap_usd)
