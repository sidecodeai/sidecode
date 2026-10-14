"""Dictation polish through the operator's OpenAI-compatible model (A29).

This is the one place the gateway talks to a language model, and it does so only on an app's
request: it lists the provider's models and sends one dictation, with the conversation the app
already shows, through ``/chat/completions``. The text and the conversation live in memory for the
length of that call. They are never logged, never stored and never forwarded to a device — the
answer goes back to the app that asked, as a draft in its composer.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import PolishConfig
from .logging import logger
from .polish_prompt import ContextMessage, build_messages, clean_answer

log = logger("rc_gateway.polish")

#: The model list changes rarely and every Settings screen asks for it, so it is cached this long.
MODELS_CACHE_SECONDS = 600.0
TEMPERATURE = 0.2
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS = 4096
#: Statuses that mean "this provider has no model index", as opposed to a provider in trouble.
_NO_MODELS_STATUSES = (404, 405)


class PolishError(RuntimeError):
    def __init__(self, message: str, code: str = "upstream") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PolishModel:
    id: str
    label: str


class Polisher(Protocol):
    async def models(self) -> list[PolishModel]: ...

    async def polish(
        self,
        text: str,
        *,
        model: str,
        strength: str,
        language: str | None,
        context: Sequence[ContextMessage],
    ) -> str: ...


class PolishClient:
    """An OpenAI-compatible provider: ``GET /models`` and ``POST /chat/completions``."""

    def __init__(
        self,
        config: PolishConfig,
        client: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._cached: list[PolishModel] | None = None
        self._cached_at = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=False)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def models(self) -> list[PolishModel]:
        """The models an app may choose from, cached for ten minutes."""
        now = self._clock()
        if self._cached is not None and now - self._cached_at < MODELS_CACHE_SECONDS:
            return list(self._cached)
        found = self._allowed(await self._fetch_model_ids())
        self._cached = found
        self._cached_at = now
        return list(found)

    async def polish(
        self,
        text: str,
        *,
        model: str,
        strength: str,
        language: str | None,
        context: Sequence[ContextMessage],
    ) -> str:
        """Send one dictation through the model and return the polished text."""
        body: dict[str, Any] = {
            "model": model,
            "messages": build_messages(text, strength=strength, language=language, context=context),
            "temperature": TEMPERATURE,
            "max_tokens": _max_output_tokens(text),
            "stream": False,
        }
        try:
            response = await self._http().post(
                f"{self.config.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
                timeout=self.config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        answer = clean_answer(_completion_text(_decoded(response)))
        if not answer:
            log.warning("polish provider returned an empty answer")
            raise PolishError("the polish provider returned an empty answer")
        return answer

    async def _fetch_model_ids(self) -> list[str]:
        """The provider's model index, or the operator's allowlist when it has none."""
        try:
            response = await self._http().get(
                f"{self.config.base_url}/models",
                headers=self._headers(),
                timeout=self.config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        if response.status_code in _NO_MODELS_STATUSES and self.config.models:
            # A provider with no model index is usable as long as the operator named the models.
            return list(self.config.models)
        return _model_ids(_decoded(response))

    def _allowed(self, ids: Sequence[str]) -> list[PolishModel]:
        """Filter and order by ``POLISH_MODELS``; an empty result falls back to the allowlist."""
        if not self.config.models:
            return [PolishModel(id=item, label=item) for item in ids]
        offered = set(ids)
        kept = [item for item in self.config.models if item in offered]
        return [PolishModel(id=item, label=item) for item in kept or self.config.models]


def _max_output_tokens(text: str) -> int:
    """A ceiling that follows the input: a polished dictation is never a longer document.

    One token per character is a safe upper bound in every language the apps offer, so the
    dictation's own length bounds what the provider may bill and return.
    """
    return min(MAX_OUTPUT_TOKENS, max(MIN_OUTPUT_TOKENS, len(text) + MIN_OUTPUT_TOKENS))


def _unreachable(exc: httpx.HTTPError) -> PolishError:
    log.warning("polish provider unreachable", error=type(exc).__name__)
    return PolishError("the polish provider is unreachable")


def _decoded(response: httpx.Response) -> Any:
    """Return the decoded body, or raise the ``PolishError`` the caller should surface."""
    if response.status_code >= 400:
        log.warning("polish provider rejected the request", status=response.status_code)
        raise PolishError(f"the polish provider returned {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise PolishError("the polish provider returned a non-JSON body") from exc


def _model_ids(payload: Any) -> list[str]:
    """Read ``data[].id`` out of an OpenAI-compatible model index."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        identifier = item.get("id") if isinstance(item, dict) else None
        if isinstance(identifier, str) and identifier and identifier not in ids:
            ids.append(identifier)
    return ids


def _completion_text(payload: Any) -> str:
    """Read ``choices[0].message.content`` out of a chat completion."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""
