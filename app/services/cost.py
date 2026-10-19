from __future__ import annotations

from dataclasses import dataclass, field

# Approximate OpenAI prices (USD). Kept here so one edit updates every estimate.
# Transcription is per audio-minute; chat models are per 1M tokens (in/out).
TRANSCRIBE_USD_PER_MIN: dict[str, float] = {
    "whisper-1": 0.006,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}

CHAT_USD_PER_1M: dict[str, tuple[float, float]] = {
    # model: (input, output)
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}

_DEFAULT_CHAT = (0.50, 1.50)
_DEFAULT_TRANSCRIBE = 0.006


def transcription_cost(model: str, minutes: float) -> float:
    rate = TRANSCRIBE_USD_PER_MIN.get(model, _DEFAULT_TRANSCRIBE)
    return rate * minutes


def chat_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rate_in, rate_out = CHAT_USD_PER_1M.get(model, _DEFAULT_CHAT)
    return (tokens_in / 1_000_000) * rate_in + (tokens_out / 1_000_000) * rate_out


class CostCapExceeded(RuntimeError):
    def __init__(self, spent: float, cap: float):
        super().__init__(
            f"Estimated cost ${spent:.2f} exceeded the per-job cap of ${cap:.2f}. "
            "Job stopped. Raise MAX_JOB_COST_USD or reduce clip count / video length."
        )
        self.spent = spent
        self.cap = cap


@dataclass
class CostTracker:
    """Accumulates per-job cost and enforces a hard cap."""

    cap_usd: float
    audio_minutes: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    models_used: dict[str, float] = field(default_factory=dict)
    _total: float = 0.0

    def add_transcription(self, model: str, minutes: float) -> None:
        self.audio_minutes += minutes
        cost = transcription_cost(model, minutes)
        self._total += cost
        self.models_used[model] = self.models_used.get(model, 0.0) + cost
        self._enforce()

    def add_chat(self, model: str, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        cost = chat_cost(model, tokens_in, tokens_out)
        self._total += cost
        self.models_used[model] = self.models_used.get(model, 0.0) + cost
        self._enforce()

    @property
    def total_usd(self) -> float:
        return round(self._total, 4)

    def _enforce(self) -> None:
        if self._total > self.cap_usd:
            raise CostCapExceeded(self._total, self.cap_usd)
