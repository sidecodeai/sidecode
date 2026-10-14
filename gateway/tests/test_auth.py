"""Login, cookie and bearer credentials, the Origin rule, rate limiting and logout."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from rc_gateway.auth import (
    SESSION_COOKIE_NAME,
    authenticate_login,
    make_session_token,
    session_token_claims,
)
from rc_gateway.auth_store import StoredSession
from rc_gateway.state import GatewayState

from .conftest import ORIGIN, PASSWORD, sign_in


def _login(client: TestClient, password: str, *, origin: str | None = None) -> Any:
    headers = {"Origin": origin} if origin else {}
    return client.post(
        "/api/login", json={"username": "admin", "password": password}, headers=headers
    )


def test_health_needs_no_credential(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["protocol"] == 1
    assert body["auth"] == {"mode": "password", "registration_open": False}


def test_login_sets_cookie_and_returns_token(client: TestClient) -> None:
    response = _login(client, PASSWORD, origin=ORIGIN)
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {"username": "admin", "role": "admin"}
    assert body["exp"] > time.time()
    assert SESSION_COOKIE_NAME in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()


def test_login_rejects_a_wrong_password(client: TestClient) -> None:
    response = _login(client, "nope", origin=ORIGIN)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_login_requires_a_matching_origin(client: TestClient) -> None:
    response = _login(client, PASSWORD, origin="https://evil.example")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_login_without_an_origin_is_allowed(client: TestClient) -> None:
    """Native apps send no Origin; login is where they obtain their bearer token."""
    response = _login(client, PASSWORD)
    assert response.status_code == 200
    assert response.json()["token"]


def test_login_is_rate_limited(client: TestClient) -> None:
    for _ in range(5):
        _login(client, "nope", origin=ORIGIN)
    blocked = _login(client, PASSWORD, origin=ORIGIN)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_requests"


def test_bearer_token_works_without_an_origin(client: TestClient, auth: dict[str, str]) -> None:
    client.cookies.clear()
    assert client.get("/api/session", headers=auth).status_code == 200
    assert client.get("/api/devices", headers=auth).status_code == 200


def test_cookie_mutation_without_origin_is_refused(client: TestClient, token: str) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/api/devices").status_code == 200
    refused = client.post("/api/devices/pairing")
    assert refused.status_code == 403
    allowed = client.post("/api/devices/pairing", headers={"Origin": ORIGIN})
    assert allowed.status_code == 200


def test_logout_revokes_the_token(client: TestClient, auth: dict[str, str]) -> None:
    assert client.post("/api/logout", headers=auth).status_code == 200
    client.cookies.clear()
    assert client.get("/api/session", headers=auth).status_code == 401


def test_unknown_token_is_unauthorized(client: TestClient) -> None:
    response = client.get("/api/session", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401


def test_a_forged_signature_is_rejected(state: GatewayState) -> None:
    token, _ = make_session_token(state.config.secret, 60)
    payload, _ = token.split(".", 1)
    assert session_token_claims(f"{payload}.tampered", state.config.secret) is None


def test_expired_tokens_do_not_authenticate(client: TestClient, state: GatewayState) -> None:
    token, _ = make_session_token(state.config.secret, -10)
    response = client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_unknown_username_fails_like_a_wrong_password(state: GatewayState) -> None:
    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await authenticate_login(state.users, "someone", PASSWORD, PASSWORD),
            await authenticate_login(state.users, "admin", "wrong", PASSWORD),
            await authenticate_login(state.users, "admin", PASSWORD, PASSWORD),
        )

    unknown, wrong, correct = asyncio.run(scenario())
    assert unknown == "unauthorized"
    assert wrong == "unauthorized"
    assert correct.username == "admin"
    assert correct.role == "admin"


def test_a_login_reports_the_account_it_signed_in_as(client: TestClient) -> None:
    """The username the caller sent decides the account, not a default (A24)."""
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 400
    token = sign_in(client, "ADMIN", PASSWORD)
    body = client.get("/api/session", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["user"] == {"username": "admin", "role": "admin"}


def test_config_reports_capabilities(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/api/config", headers=auth).json()
    assert body["public_origin"] == ORIGIN
    # A44: the provider detects the language, so there is only ever `auto` to report.
    assert body["stt"] == {"enabled": True, "languages": ["auto"]}
    assert body["push"]["web_enabled"] is True
    assert body["push"]["apns_enabled"] is False


def test_a_native_client_may_log_in_without_an_origin(client: TestClient) -> None:
    """Only browsers send Origin, and the bearer token is obtained here."""
    response = _login(client, PASSWORD)
    assert response.status_code == 200
    assert response.json()["user"] == {"username": "admin", "role": "admin"}


def test_a_session_survives_a_gateway_restart(tmp_path: Path) -> None:
    """Review finding 9: a container update must not sign every client out."""
    from rc_gateway.app import build_state, create_app

    from .conftest import make_config

    config = make_config(tmp_path)
    with TestClient(create_app(build_state(config))) as first:
        token = sign_in(first, "admin", PASSWORD)
        alive = first.get("/api/session", headers={"Authorization": f"Bearer {token}"})
        assert alive.status_code == 200

    # A brand new process over the same DATA_DIR, as a restart produces.
    with TestClient(create_app(build_state(config))) as second:
        response = second.get("/api/session", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["user"] == {"username": "admin", "role": "admin"}
        with second.websocket_connect(
            "/ws/app", headers={"Authorization": f"Bearer {token}"}
        ) as app:
            assert app.receive_json()["type"] == "hello"


def test_a_logout_survives_a_restart(tmp_path: Path) -> None:
    from rc_gateway.app import build_state, create_app

    from .conftest import make_config

    config = make_config(tmp_path)
    with TestClient(create_app(build_state(config))) as first:
        token = sign_in(first, "admin", PASSWORD)
        signed_out = first.post("/api/logout", headers={"Authorization": f"Bearer {token}"})
        assert signed_out.status_code == 200

    with TestClient(create_app(build_state(config))) as second:
        assert (
            second.get("/api/session", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )


def test_an_expired_row_does_not_authenticate_after_a_restart(tmp_path: Path) -> None:
    from rc_gateway.app import build_state, create_app
    from rc_gateway.auth import make_session_token, session_token_claims

    from .conftest import make_config

    config = make_config(tmp_path)
    state = build_state(config)
    token, _ = make_session_token(config.secret, -10)
    claims = session_token_claims(token, config.secret)
    assert claims is not None
    asyncio.run(state.auth_store.add(StoredSession(claims.jti, claims.username, claims.expires_at)))

    with TestClient(create_app(build_state(config))) as restarted:
        assert (
            restarted.get("/api/session", headers={"Authorization": f"Bearer {token}"}).status_code
            == 401
        )
    # Startup pruning removes the row rather than leaving it to accumulate.
    assert asyncio.run(build_state(config).auth_store.count()) == 0


def test_many_logins_evict_the_coldest_instead_of_failing(tmp_path: Path) -> None:
    """The old registry refused a new login past its cap and answered 503."""
    from rc_gateway.app import build_state
    from rc_gateway.auth import make_session_token, session_token_claims

    from .conftest import make_config

    config = make_config(tmp_path)
    state = build_state(config)
    state.sessions.cap = 4

    async def scenario() -> tuple[bool, bool]:
        first = session_token_claims(make_session_token(config.secret, 3600)[0], config.secret)
        assert first is not None
        await state.sessions.register(first)
        for _ in range(12):
            claims = session_token_claims(make_session_token(config.secret, 3600)[0], config.secret)
            assert claims is not None
            await state.sessions.register(claims)
        # Evicted from the cache, but the durable row still authenticates it.
        return len(state.sessions._entries) <= 4, await state.sessions.active(first)

    bounded, still_valid = asyncio.run(scenario())
    assert bounded
    assert still_valid
