"""Settings, the mount, and the status route. The call itself is ``test_ws.py``."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rc_gateway import live
from rc_gateway.app import create_app
from rc_gateway.live.config import LiveConfig, LiveConfigError, load_live_config
from rc_gateway.live.meter import NoMeter
from rc_gateway.live.policy import Admission, LiveStatus
from rc_gateway.state import GatewayState

from ..conftest import PASSWORD, add_member, sign_in

BASE = {"LIVE_ASSISTANT_ENABLED": "1", "LIVE_OPENAI_API_KEY": "k"}


class OpenPolicy:
    """A stand-in for a gateway's own policy: whoever asks is let in, and nothing is metered."""

    async def status(self, username: str) -> LiveStatus:
        return LiveStatus(enabled=True, available=True, reason=None)

    async def admit(self, username: str) -> Admission:
        return Admission(NoMeter())


def test_the_feature_is_off_unless_enabled_and_given_a_key() -> None:
    assert load_live_config({}) is None
    assert load_live_config({"LIVE_ASSISTANT_ENABLED": "true"}) is None
    assert load_live_config({"LIVE_OPENAI_API_KEY": "k"}) is None
    assert load_live_config({**BASE, "LIVE_ASSISTANT_ENABLED": "false"}) is None
    assert load_live_config(BASE) is not None


def test_the_defaults() -> None:
    config = load_live_config(BASE)
    assert config is not None
    assert config.model == "gpt-live-1"
    assert (config.idle_seconds, config.max_call_seconds) == (90.0, 1200.0)
    assert config.allowed_users == frozenset()
    assert (config.max_concurrent_calls, config.cooldown_seconds) == (2, 10.0)
    assert config.daily_seconds == 7200.0
    assert config.voice == ""
    assert config.barge_in is True


def test_every_setting_is_read_from_the_environment() -> None:
    config = load_live_config(
        {
            **BASE,
            "LIVE_OPENAI_BASE_URL": "https://proxy.example/",
            "LIVE_MODEL": "m",
            "LIVE_BACKEND_MODEL": "b",
            "LIVE_VOICE": "marin",
            "LIVE_IDLE_TIMEOUT_SECONDS": "30",
            "LIVE_MAX_CALL_SECONDS": "300",
            "LIVE_DAILY_SECONDS": "3600",
            "LIVE_ALLOWED_USERS": " a, B ,,",
            "LIVE_MAX_CONCURRENT_CALLS": "5",
            "LIVE_COOLDOWN_SECONDS": "0",
            "LIVE_BARGE_IN": "off",
        }
    )
    assert config == LiveConfig(
        api_key="k",
        base_url="https://proxy.example",
        model="m",
        backend_model="b",
        voice="marin",
        idle_seconds=30.0,
        max_call_seconds=300.0,
        daily_seconds=3600.0,
        allowed_users=frozenset({"a", "b"}),
        max_concurrent_calls=5,
        cooldown_seconds=0.0,
        barge_in=False,
    )


@pytest.mark.parametrize("raw", ["0", "-5", "soon", ""])
def test_zero_or_invalid_limits_fall_back_to_the_defaults(raw: str) -> None:
    config = load_live_config(
        {
            **BASE,
            "LIVE_IDLE_TIMEOUT_SECONDS": raw,
            "LIVE_MAX_CALL_SECONDS": raw,
            "LIVE_DAILY_SECONDS": raw,
        }
    )
    assert config is not None
    assert (config.idle_seconds, config.max_call_seconds) == (90.0, 1200.0)
    assert config.daily_seconds == 7200.0


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com",
        "https://proxy.example/openai/",
        "http://127.0.0.1:9",
        "http://127.8.9.10",
        "http://localhost:8080",
        "http://[::1]:9",
    ],
)
def test_a_base_url_with_tls_or_on_the_loopback_is_accepted(url: str) -> None:
    config = load_live_config({**BASE, "LIVE_OPENAI_BASE_URL": url})
    assert config is not None
    assert config.base_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com",
        "http://10.0.0.5:8080",
        "http://192.168.1.2",
        "http://example.com",
        "ws://example.com",
        "ftp://127.0.0.1",
        "https://user:secret@example.com",
        "not a url",
        "https://",
        "127.0.0.1:9",
    ],
)
def test_a_base_url_that_would_send_the_key_in_the_clear_or_nowhere_is_refused(url: str) -> None:
    with pytest.raises(LiveConfigError):
        load_live_config({**BASE, "LIVE_OPENAI_BASE_URL": url})


def test_a_refused_url_names_the_setting_and_not_the_key() -> None:
    with pytest.raises(LiveConfigError) as refused:
        load_live_config(
            {**BASE, "LIVE_OPENAI_API_KEY": "sk-secret", "LIVE_OPENAI_BASE_URL": "http://x.example"}
        )
    assert "LIVE_OPENAI_BASE_URL" in str(refused.value)
    assert "sk-secret" not in str(refused.value)


def test_the_feature_stays_off_and_says_why_when_the_url_is_unsafe(
    monkeypatch: pytest.MonkeyPatch, state: GatewayState, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LIVE_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("LIVE_OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("LIVE_OPENAI_BASE_URL", "http://api.example.com")
    monkeypatch.setenv("LIVE_ALLOWED_USERS", "admin")
    with (
        caplog.at_level(logging.ERROR, logger="rc_gateway.live"),
        TestClient(create_app(state)) as client,
    ):
        headers = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        assert client.get("/api/live/status", headers=headers).status_code == 404
    [record] = [r for r in caplog.records if r.getMessage() == "live assistant stays off"]
    assert "loopback" in record.fields["reason"]  # type: ignore[attr-defined]
    assert "sk-secret" not in repr(record.__dict__)


def test_the_socket_is_the_live_apis_and_names_no_model() -> None:
    assert LiveConfig(api_key="k").socket_url == "wss://api.openai.com/v1/live/sessions"
    local = LiveConfig(api_key="k", base_url="http://127.0.0.1:9")
    assert local.socket_url == "ws://127.0.0.1:9/v1/live/sessions"


def live_paths(app: FastAPI) -> list[str]:
    """The live assistant's HTTP routes the app serves (its socket is not in the schema)."""
    return [path for path in app.openapi()["paths"] if "live" in path]


def test_a_gateway_with_a_hosted_service_mounts_it_only_under_a_policy_of_its_own() -> None:
    # A stand-in for the state of a gateway with a hosted service, so this test needs none of its
    # code; that gateway's own tests check the real state.
    app = FastAPI()
    hosted = cast(GatewayState, SimpleNamespace(paid=object()))
    assert live.mount_live(app, hosted, config=LiveConfig(api_key="k")) is None
    assert not hasattr(app.state, "live")
    assert live_paths(app) == []

    governed = FastAPI()
    service = live.mount_live(governed, hosted, config=LiveConfig(api_key="k"), policy=OpenPolicy())
    assert service is not None
    assert governed.state.live is service
    assert live_paths(governed) == ["/api/live/status"]


def test_a_state_with_no_hosted_service_at_all_is_a_self_hosted_one() -> None:
    app = FastAPI()
    state = cast(GatewayState, SimpleNamespace())
    service = live.mount_live(app, state, config=LiveConfig(api_key="k"))
    assert service is not None
    assert app.state.live is service
    assert live_paths(app) == ["/api/live/status"]


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch, state: GatewayState) -> Iterator[TestClient]:
    monkeypatch.setenv("LIVE_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("LIVE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("LIVE_ALLOWED_USERS", "admin")
    with TestClient(create_app(state)) as client:
        yield client


def test_nothing_is_mounted_while_it_is_off(client: TestClient, auth: dict[str, str]) -> None:
    assert client.get("/api/live/status", headers=auth).status_code == 404


def test_the_status_route_needs_a_login(enabled: TestClient) -> None:
    assert enabled.get("/api/live/status").status_code == 401


def test_the_status_says_available_for_a_listed_account(enabled: TestClient) -> None:
    headers = {"Authorization": f"Bearer {sign_in(enabled, 'admin', PASSWORD)}"}
    response = enabled.get("/api/live/status", headers=headers)
    assert response.json() == {"enabled": True, "available": True, "reason": None}
    assert response.headers["cache-control"] == "no-store"


def test_the_status_says_off_for_an_account_that_is_not_listed(enabled: TestClient) -> None:
    admin = {"Authorization": f"Bearer {sign_in(enabled, 'admin', PASSWORD)}"}
    erin = add_member(enabled, admin, "erin")
    assert enabled.get("/api/live/status", headers=erin).json() == {
        "enabled": False,
        "available": False,
        "reason": "not_allowed",
    }


def test_an_empty_allowlist_means_nobody(
    monkeypatch: pytest.MonkeyPatch, state: GatewayState
) -> None:
    monkeypatch.setenv("LIVE_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("LIVE_OPENAI_API_KEY", "k")
    monkeypatch.delenv("LIVE_ALLOWED_USERS", raising=False)
    with TestClient(create_app(state)) as client:
        headers = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        assert client.get("/api/live/status", headers=headers).json()["enabled"] is False
