from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class LLMResponse:
    content: str
    model: str
    tokens_in: int
    tokens_out: int


class LLMClient(ABC):
    """Provider-agnostic chat interface. Other providers slot in behind this."""

    @abstractmethod
    def complete_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 1024
    ) -> LLMResponse: ...


class OpenAILLMClient(LLMClient):
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _client(self):
        from openai import OpenAI

        return OpenAI(api_key=self.settings.openai_api_key)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
    )
    def complete_json(
        self, *, model: str, system: str, user: str, max_tokens: int = 1024
    ) -> LLMResponse:
        client = self._client()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=max_tokens,
        )
        usage = resp.usage
        return LLMResponse(
            content=resp.choices[0].message.content or "",
            model=model,
            tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
            tokens_out=getattr(usage, "completion_tokens", 0) or 0,
        )


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object from an LLM response, tolerating code fences and
    surrounding prose. Raises ValueError if no object is found."""
    text = raw.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fences.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError(f"No JSON object found in LLM response: {raw[:200]!r}") from None


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    return OpenAILLMClient(settings or get_settings())
