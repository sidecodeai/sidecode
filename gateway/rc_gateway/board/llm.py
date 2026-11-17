"""The one model call of a board update: an OpenAI-compatible `/chat/completions` (A62).

Two messages, no streaming, a low temperature and a fixed ceiling on the answer. What comes back
is text for `prompt.parse`; the call itself only says whether there was an answer. Neither the
messages nor the answer are ever logged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..logging import logger
from .limits import MAX_OUTPUT_TOKENS, MODEL_TIMEOUT_SECONDS, TEMPERATURE

log = logger("rc_gateway.board.llm")

Message = Mapping[str, str]


class ModelFailure(RuntimeError):
    """The model could not be asked, or gave no usable answer: the update failed, not the board."""


@dataclass(frozen=True)
class Completion:
    text: str
    #: What the provider reported in `usage`, when it reported it.
    input_tokens: int | None = None
    output_tokens: int | None = None


class BoardModel(Protocol):
    async def complete(self, messages: Sequence[Message]) -> Completion: ...


class ChatModel:
    """One endpoint, one key and one model, asked once per update."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=MODEL_TIMEOUT_SECONDS, trust_env=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def body(self, messages: Sequence[Message]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "stream": False,
        }

    async def complete(self, messages: Sequence[Message]) -> Completion:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = await self._http().post(
                f"{self.base_url}/chat/completions",
                json=self.body(messages),
                headers=headers,
                timeout=MODEL_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            log.warning("board model unreachable", error=type(exc).__name__)
            raise ModelFailure("the board model is unreachable") from exc
        if response.status_code >= 400:
            log.warning("board model refused the request", status=response.status_code)
            raise ModelFailure(f"the board model answered {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelFailure("the board model answered with something other than JSON") from exc
        text = _content(payload)
        if not text.strip():
            raise ModelFailure("the board model returned an empty answer")
        prompt_tokens, completion_tokens = _usage(payload)
        return Completion(text, prompt_tokens, completion_tokens)


def _content(payload: Any) -> str:
    """``choices[0].message.content`` of a chat completion."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


def _usage(payload: Any) -> tuple[int | None, int | None]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None, None
    return _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens"))


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
