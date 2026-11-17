"""A62: the board's one model call, an OpenAI-compatible chat completion."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from rc_gateway.board.limits import MAX_OUTPUT_TOKENS, MODEL_TIMEOUT_SECONDS, TEMPERATURE
from rc_gateway.board.llm import ChatModel, Completion, ModelFailure

MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "board"}]


def model(handler: Any) -> tuple[ChatModel, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return ChatModel("https://models.invalid/v1/", "sk-board", "board-model", client), seen


def completion(content: str, usage: dict[str, Any] | None = None) -> httpx.Response:
    body: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


async def test_the_request_is_two_messages_without_streaming() -> None:
    chat, seen = model(lambda request: completion('{"items": []}'))
    answer = await chat.complete(MESSAGES)
    assert answer == Completion('{"items": []}')
    (request,) = seen
    assert request.url == "https://models.invalid/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-board"
    assert json.loads(request.content) == {
        "model": "board-model",
        "messages": MESSAGES,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    assert (TEMPERATURE, MAX_OUTPUT_TOKENS) == (0.1, 1500)
    assert request.extensions["timeout"]["read"] == MODEL_TIMEOUT_SECONDS == 45


async def test_usage_is_read_when_the_provider_reports_it() -> None:
    usage = {"prompt_tokens": 3100, "completion_tokens": 240, "total_tokens": 3340}
    chat, _ = model(lambda request: completion("{}", usage))
    assert await chat.complete(MESSAGES) == Completion("{}", 3100, 240)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": {"message": "boom"}}),
        httpx.Response(404, json={}),
        httpx.Response(200, text="not json"),
        completion(""),
        completion("   "),
        httpx.Response(200, json={"choices": []}),
    ],
)
async def test_a_failed_or_empty_answer_is_a_model_failure(response: httpx.Response) -> None:
    chat, _ = model(lambda request: response)
    with pytest.raises(ModelFailure):
        await chat.complete(MESSAGES)


async def test_a_timeout_or_an_unreachable_provider_is_a_model_failure() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    def gone(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler in (slow, gone):
        chat, _ = model(handler)
        with pytest.raises(ModelFailure):
            await chat.complete(MESSAGES)


async def test_a_model_closes_only_the_client_it_made() -> None:
    shared = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: completion("{}")))
    await ChatModel("https://models.invalid", "", "m", shared).close()
    assert not shared.is_closed
    own = ChatModel("https://models.invalid", "", "m")
    own._http()
    await own.close()
    assert own._client is None
    await shared.aclose()
