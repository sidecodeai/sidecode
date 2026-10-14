"""Dictation polish: configuration, the prompt, the provider client and the two routes (A29)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.config import ConfigError, PolishConfig, load_config
from rc_gateway.polish import (
    MAX_OUTPUT_TOKENS,
    MODELS_CACHE_SECONDS,
    PolishClient,
    PolishError,
)
from rc_gateway.polish_prompt import (
    DICTATION_PREFIX,
    ContextMessage,
    build_messages,
    clean_answer,
    system_text,
)

from .conftest import FIXTURE_DIR, FakePolisher, drain_until, make_config

MODELS_PATH = "/api/polish/models"
POLISH_PATH = "/api/polish"


def fixture(name: str) -> Any:
    return json.loads((FIXTURE_DIR / "http" / name).read_text(encoding="utf-8"))


def polish_config(**overrides: Any) -> PolishConfig:
    values: dict[str, Any] = {
        "base_url": "https://polish.example/v1",
        "api_key": "secret",
        "models": (),
        "timeout_seconds": 20.0,
    }
    values.update(overrides)
    return PolishConfig(**values)


def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **values: str) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)
    for name in ("POLISH_BASE_URL", "POLISH_API_KEY", "POLISH_MODELS", "POLISH_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


# --- E1 configuration ---------------------------------------------------------------------


def test_polish_needs_both_a_base_url_and_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env(monkeypatch, tmp_path)
    assert load_config(load_env_file=False).polish.enabled is False

    env(monkeypatch, tmp_path, POLISH_BASE_URL="https://polish.example/v1/")
    assert load_config(load_env_file=False).polish.enabled is False

    env(monkeypatch, tmp_path, POLISH_API_KEY="secret")
    assert load_config(load_env_file=False).polish.enabled is False

    env(
        monkeypatch,
        tmp_path,
        POLISH_BASE_URL="https://polish.example/v1/",
        POLISH_API_KEY="secret",
    )
    polish = load_config(load_env_file=False).polish
    assert polish.enabled is True
    # The trailing slash is dropped once, here, so every request path is built the same way.
    assert polish.base_url == "https://polish.example/v1"
    assert polish.models == ()
    assert polish.timeout_seconds == 20.0


def test_the_allowlist_and_the_timeout_are_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env(
        monkeypatch,
        tmp_path,
        POLISH_BASE_URL="https://polish.example/v1",
        POLISH_API_KEY="secret",
        POLISH_MODELS=" gpt-4.1-mini , gpt-4.1 ,, gpt-4.1 ",
        POLISH_TIMEOUT_SECONDS="35",
    )
    polish = load_config(load_env_file=False).polish
    assert polish.models == ("gpt-4.1-mini", "gpt-4.1")
    assert polish.timeout_seconds == 35.0

    env(monkeypatch, tmp_path, POLISH_TIMEOUT_SECONDS="soon")
    with pytest.raises(ConfigError) as invalid:
        load_config(load_env_file=False)
    assert "POLISH_TIMEOUT_SECONDS" in str(invalid.value)

    env(monkeypatch, tmp_path, POLISH_TIMEOUT_SECONDS="0")
    with pytest.raises(ConfigError):
        load_config(load_env_file=False)


# --- E2 the prompt ------------------------------------------------------------------------


def test_moderate_keeps_the_words_and_strong_may_restructure() -> None:
    moderate = system_text("moderate")
    strong = system_text("strong")
    for text in (moderate, strong):
        assert "fillers" in text and "false starts" in text and "punctuate" in text
        assert "Return the polished text only" in text
        assert "language the text was spoken in" in text
    assert "keep the speaker's words and their order" in moderate
    assert "restructure" not in moderate
    assert "restructure for clarity and precision" in strong
    assert "resolve vague references" in strong
    assert "adding no request the speaker did not make" in strong
    assert "The speaker chose the language en." in system_text("strong", "en")
    assert "chose the language" not in system_text("strong", "auto")


def test_the_dictation_is_the_last_message_and_the_conversation_comes_first() -> None:
    request = fixture("polish.request.json")
    context = [ContextMessage(role=item["role"], text=item["text"]) for item in request["context"]]
    messages = build_messages(
        request["text"],
        strength=request["strength"],
        language=request["language"],
        context=context,
    )
    assert messages[0]["role"] == "system"
    assert [item["role"] for item in messages[1:-1]] == ["user", "assistant"]
    assert messages[1]["content"] == request["context"][0]["text"]
    assert messages[2]["content"] == request["context"][1]["text"]
    last = messages[-1]
    assert last["role"] == "user"
    assert last["content"] == f"{DICTATION_PREFIX}\n{request['text']}"


def test_the_prompt_trims_to_the_schema_limits() -> None:
    context = [ContextMessage(role="user", text=f"turn {index}") for index in range(30)]
    context.append(ContextMessage(role="assistant", text="x" * 5000))
    context.append(ContextMessage(role="assistant", text="   "))
    messages = build_messages("a" * 9000, strength="moderate", context=context)
    # The last twenty items are taken first, then the blank one is dropped: 19 turns remain.
    turns = messages[1:-1]
    assert len(turns) == 19
    assert turns[0]["content"] == "turn 12"
    assert len(turns[-1]["content"]) == 4000
    assert len(messages[-1]["content"]) == 8192 + len(DICTATION_PREFIX) + 1


def test_an_answer_loses_one_surrounding_quote_pair() -> None:
    assert clean_answer('  "Stay solid green."  ') == "Stay solid green."
    assert clean_answer("“Stay solid green.”") == "Stay solid green."
    assert clean_answer('Rename "green" to "idle".') == 'Rename "green" to "idle".'
    assert clean_answer("") == ""


# --- E2 the provider client ---------------------------------------------------------------


def mock_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_models_list_is_read_and_cached() -> None:
    calls: list[str] = []
    clock = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("Authorization") == "Bearer secret"
        return httpx.Response(200, json={"data": [{"id": "gpt-4.1"}, {"id": "gpt-4.1-mini"}]})

    async with mock_client(handler) as http:
        client = PolishClient(polish_config(), http, clock=lambda: clock[0])
        first = await client.models()
        clock[0] = MODELS_CACHE_SECONDS - 1
        cached = await client.models()
        clock[0] = MODELS_CACHE_SECONDS + 1
        await client.models()

    assert [item.id for item in first] == ["gpt-4.1", "gpt-4.1-mini"]
    assert [item.label for item in first] == ["gpt-4.1", "gpt-4.1-mini"]
    assert cached == first
    assert calls == ["https://polish.example/v1/models"] * 2


@pytest.mark.asyncio
async def test_the_allowlist_filters_orders_and_stands_in_for_a_missing_index() -> None:
    def listing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "gpt-4.1-mini"}, {"id": "other"}]})

    config = polish_config(models=("gpt-4.1", "gpt-4.1-mini"))
    async with mock_client(listing) as http:
        filtered = await PolishClient(config, http).models()
    assert [item.id for item in filtered] == ["gpt-4.1-mini"]

    for status in (404, 405):

        def no_index(request: httpx.Request, code: int = status) -> httpx.Response:
            return httpx.Response(code)

        async with mock_client(no_index) as http:
            served = await PolishClient(config, http).models()
        assert [item.id for item in served] == ["gpt-4.1", "gpt-4.1-mini"]

    async with mock_client(lambda request: httpx.Response(200, json={"data": []})) as http:
        empty = await PolishClient(config, http).models()
    assert [item.id for item in empty] == ["gpt-4.1", "gpt-4.1-mini"]

    # Without an allowlist there is nothing to stand in with, so the failure stays a failure.
    async with mock_client(lambda request: httpx.Response(404)) as http:
        with pytest.raises(PolishError) as missing:
            await PolishClient(polish_config(), http).models()
    assert missing.value.code == "upstream"


@pytest.mark.asyncio
async def test_a_completion_carries_the_prompt_and_returns_the_text() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '  "Stay solid green."  '}}]}
        )

    async with mock_client(handler) as http:
        polished = await PolishClient(polish_config(), http).polish(
            "um the green blinking thing",
            model="gpt-4.1-mini",
            strength="strong",
            language="en",
            context=[ContextMessage(role="user", text="Make the dot pulse.")],
        )

    assert polished == "Stay solid green."
    assert seen["url"] == "https://polish.example/v1/chat/completions"
    body = seen["body"]
    assert body["model"] == "gpt-4.1-mini"
    assert body["temperature"] == 0.2
    assert body["stream"] is False
    assert 0 < body["max_tokens"] <= MAX_OUTPUT_TOKENS
    assert body["messages"][-1]["content"].startswith(DICTATION_PREFIX)


@pytest.mark.asyncio
async def test_an_empty_answer_a_bad_status_and_a_timeout_are_upstream_failures() -> None:
    async with mock_client(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})
    ) as http:
        with pytest.raises(PolishError) as empty:
            await _polish(PolishClient(polish_config(), http))
    assert empty.value.code == "upstream"

    async with mock_client(lambda request: httpx.Response(500, json={"error": "boom"})) as http:
        with pytest.raises(PolishError):
            await _polish(PolishClient(polish_config(), http))

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with mock_client(timeout) as http:
        with pytest.raises(PolishError) as timed_out:
            await _polish(PolishClient(polish_config(), http))
    assert timed_out.value.code == "upstream"

    async with mock_client(timeout) as http:
        with pytest.raises(PolishError):
            await PolishClient(polish_config(), http).models()


async def _polish(client: PolishClient) -> str:
    return await client.polish(
        "um the green thing", model="m", strength="moderate", language=None, context=()
    )


# --- E3 the routes ------------------------------------------------------------------------


def test_the_client_exists_only_when_the_operator_configured_one(tmp_path: Path) -> None:
    configured = build_state(make_config(tmp_path, polish=polish_config()))
    assert isinstance(configured.polisher, PolishClient)
    off = build_state(
        make_config(
            tmp_path, polish=PolishConfig(base_url="", api_key="", models=(), timeout_seconds=20.0)
        )
    )
    assert off.polisher is None


def test_the_routes_answer_unsupported_without_a_configured_model(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, polish=PolishConfig(base_url="", api_key="", models=(), timeout_seconds=20.0)
    )
    with TestClient(create_app(build_state(config))) as offline:
        token = offline.post(
            "/api/login",
            json={"username": "admin", "password": config.password},
            headers={"Origin": config.public_origin},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert offline.get("/api/config", headers=headers).json()["polish"]["enabled"] is False
        listed = offline.get(MODELS_PATH, headers=headers)
        assert listed.status_code == 503
        assert listed.json()["error"]["code"] == "unsupported"
        refused = offline.post(
            POLISH_PATH,
            json={"text": "hi", "model": "m", "strength": "moderate", "context": []},
            headers=headers,
        )
        assert refused.status_code == 503
        assert refused.json()["error"]["code"] == "unsupported"


def test_both_routes_need_a_signed_in_user(client: TestClient, polisher: FakePolisher) -> None:
    assert client.get(MODELS_PATH).status_code == 401
    assert (
        client.post(
            POLISH_PATH, json={"text": "hi", "model": "m", "strength": "moderate", "context": []}
        ).status_code
        == 401
    )
    assert polisher.calls == []


def test_the_models_route_serves_what_the_provider_offers(
    client: TestClient, auth: dict[str, str], polisher: FakePolisher
) -> None:
    from rc_gateway.polish import PolishModel

    polisher.models_offered = [
        PolishModel(id="gpt-4.1-mini", label="gpt-4.1-mini"),
        PolishModel(id="gpt-4.1", label="gpt-4.1"),
    ]
    response = client.get(MODELS_PATH, headers=auth)
    assert response.status_code == 200
    assert response.json() == fixture("polish.models.response.json")

    polisher.models_fail = "provider down"
    failed = client.get(MODELS_PATH, headers=auth)
    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "upstream"


def test_the_request_fixture_is_polished_into_the_response_fixture(
    client: TestClient, auth: dict[str, str], polisher: FakePolisher
) -> None:
    request = fixture("polish.request.json")
    expected = fixture("polish.response.json")
    polisher.text = expected["text"]
    response = client.post(POLISH_PATH, json=request, headers=auth)
    assert response.status_code == 200
    assert response.json() == expected

    call = polisher.calls[0]
    assert call["text"] == request["text"]
    assert call["model"] == request["model"]
    assert call["strength"] == request["strength"]
    assert call["language"] == request["language"]
    assert [item.role for item in call["context"]] == ["user", "assistant"]


def test_a_provider_failure_is_a_bad_gateway(
    client: TestClient, auth: dict[str, str], polisher: FakePolisher
) -> None:
    polisher.fail = "provider down"
    response = client.post(
        POLISH_PATH,
        json={"text": "hi there", "model": "m", "strength": "moderate", "context": []},
        headers=auth,
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream"


@pytest.mark.parametrize(
    "body",
    [
        {"model": "m", "strength": "moderate", "context": []},
        {"text": "", "model": "m", "strength": "moderate", "context": []},
        {"text": "   ", "model": "m", "strength": "moderate", "context": []},
        {"text": "x" * 8193, "model": "m", "strength": "moderate", "context": []},
        {"text": "hi", "model": "", "strength": "moderate", "context": []},
        {"text": "hi", "model": "m", "strength": "gentle", "context": []},
        {"text": "hi", "model": "m", "context": []},
        {"text": "hi", "model": "m", "strength": "moderate"},
        {"text": "hi", "model": "m", "strength": "moderate", "context": {}},
        {
            "text": "hi",
            "model": "m",
            "strength": "moderate",
            "context": [{"role": "user", "text": "t"}] * 21,
        },
        {
            "text": "hi",
            "model": "m",
            "strength": "moderate",
            "context": [{"role": "user", "text": "x" * 4001}],
        },
        {
            "text": "hi",
            "model": "m",
            "strength": "moderate",
            "context": [{"role": "system", "text": "t"}],
        },
        {"text": "hi", "model": "m", "strength": "moderate", "context": [], "language": "e" * 33},
    ],
)
def test_a_malformed_request_is_refused_without_calling_the_provider(
    client: TestClient, auth: dict[str, str], polisher: FakePolisher, body: dict[str, Any]
) -> None:
    response = client.post(POLISH_PATH, json=body, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert polisher.calls == []


def test_a_model_outside_the_allowlist_is_refused(tmp_path: Path, polisher: FakePolisher) -> None:
    config = make_config(
        tmp_path,
        polish=PolishConfig(
            base_url="https://polish.example/v1",
            api_key="k",
            models=("gpt-4.1-mini",),
            timeout_seconds=20.0,
        ),
    )
    with TestClient(create_app(build_state(config, polisher=polisher))) as app:
        token = app.post(
            "/api/login",
            json={"username": "admin", "password": config.password},
            headers={"Origin": config.public_origin},
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        body = {"text": "hi there", "model": "other", "strength": "moderate", "context": []}
        assert app.post(POLISH_PATH, json=body, headers=headers).status_code == 400
        body["model"] = "gpt-4.1-mini"
        assert app.post(POLISH_PATH, json=body, headers=headers).status_code == 200
    assert [call["model"] for call in polisher.calls] == ["gpt-4.1-mini"]


def test_a_burst_of_requests_is_rate_limited(
    client: TestClient, auth: dict[str, str], state: Any
) -> None:
    from rc_gateway.app import POLISH_MAX_PER_IP

    state.polish_limiter.reset()
    for _ in range(POLISH_MAX_PER_IP):
        assert client.get(MODELS_PATH, headers=auth).status_code == 200
    limited = client.get(MODELS_PATH, headers=auth)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "too_many_requests"
    state.polish_limiter.reset()


# --- E3 what hello and config report ------------------------------------------------------


def test_hello_and_config_report_polish(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/api/config", headers=auth).json()["polish"] == {"enabled": True}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        hello = drain_until(app, "hello")
    assert hello["polish"] == {"enabled": True}
