"""Close codes as a real WebSocket client sees them.

Starlette's `TestClient` synthesises a `WebSocketDisconnect` from the ASGI messages, so it reports
a close code even when the gateway never completed the handshake. That masked a real bug: refusing
the upgrade produces an HTTP 403 and no code at all, which an app cannot tell apart from the
gateway being briefly unreachable. These tests run uvicorn in-process and connect with the
`websockets` client, so the assertion is about the wire and not about the test harness.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from rc_gateway.app import create_app
from rc_gateway.auth import make_session_token
from rc_gateway.frames import CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED
from rc_gateway.state import GatewayState

from .conftest import ORIGIN


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Server:
    """A real uvicorn server on a loopback port, started and stopped per test."""

    def __init__(self, state: GatewayState) -> None:
        self.port = _free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(create_app(state), host="127.0.0.1", port=self.port, log_level="error")
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self) -> _Server:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                return self
            await asyncio.sleep(0.02)
        raise AssertionError("uvicorn did not start")

    async def __aexit__(self, *exc: object) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task


@pytest.fixture
async def server(state: GatewayState) -> AsyncIterator[_Server]:
    async with _Server(state) as running:
        yield running


async def observed_close(url: str, **kwargs: Any) -> int:
    """Connect for real and return the close code, failing if the handshake was refused."""
    try:
        async with websockets.connect(url, **kwargs) as socket_:
            await socket_.recv()
    except ConnectionClosed as closed:
        return int(closed.rcvd.code) if closed.rcvd is not None else -1
    except InvalidStatus as status:
        raise AssertionError(
            f"handshake refused with HTTP {status.response.status_code}; a client sees no close "
            "code for a handshake that never completed"
        ) from status
    raise AssertionError("the socket was not closed")


async def test_an_unauthenticated_app_socket_is_closed_4401(server: _Server) -> None:
    assert await observed_close(f"{server.url}/ws/app") == CLOSE_UNAUTHORIZED


async def test_an_expired_token_closes_the_app_socket_4401(
    server: _Server, state: GatewayState
) -> None:
    token, _ = make_session_token(state.config.secret, -10)
    code = await observed_close(
        f"{server.url}/ws/app", additional_headers={"Authorization": f"Bearer {token}"}
    )
    assert code == CLOSE_UNAUTHORIZED


async def test_a_foreign_origin_closes_the_app_socket_4403(
    server: _Server, state: GatewayState
) -> None:
    token, _ = make_session_token(state.config.secret, 3600)
    from rc_gateway.auth import session_token_claims

    claims = session_token_claims(token, state.config.secret)
    assert claims is not None
    await state.sessions.register(claims)
    code = await observed_close(
        f"{server.url}/ws/app",
        additional_headers={"Cookie": f"rc_session={token}", "Origin": "https://evil.example"},
    )
    assert code == CLOSE_FORBIDDEN


async def test_an_unknown_device_token_is_closed_4401(server: _Server) -> None:
    code = await observed_close(
        f"{server.url}/ws/device", additional_headers={"Authorization": "Bearer " + "z" * 64}
    )
    assert code == CLOSE_UNAUTHORIZED


async def test_a_valid_credential_is_accepted_over_the_wire(
    server: _Server, state: GatewayState
) -> None:
    """The same path with a good credential still reaches `hello`, cookie and Origin included."""
    token, _ = make_session_token(state.config.secret, 3600)
    from rc_gateway.auth import session_token_claims

    claims = session_token_claims(token, state.config.secret)
    assert claims is not None
    await state.sessions.register(claims)
    async with websockets.connect(
        f"{server.url}/ws/app",
        additional_headers={"Cookie": f"rc_session={token}", "Origin": ORIGIN},
    ) as socket_:
        import json

        hello = json.loads(await socket_.recv())
    assert hello["type"] == "hello"
    assert hello["protocol"] == 1


async def test_a_flood_of_anonymous_app_upgrades_stops_being_completed(
    server: _Server, state: GatewayState
) -> None:
    """GW-13: `/ws/device` has had this valve since a gateway saw 685 refusals in three hours."""
    from rc_gateway.rejects import FLOOD_ATTEMPTS

    for _ in range(FLOOD_ATTEMPTS):
        assert await observed_close(f"{server.url}/ws/app") == CLOSE_UNAUTHORIZED
    with pytest.raises(InvalidStatus):
        async with websockets.connect(f"{server.url}/ws/app"):
            pass
    # The two upgrades count separately, and STT has the same valve.
    assert await observed_close(f"{server.url}/ws/stt") == CLOSE_UNAUTHORIZED


async def test_the_stt_upgrade_has_its_own_flood_valve(
    server: _Server, state: GatewayState
) -> None:
    from rc_gateway.rejects import FLOOD_ATTEMPTS

    state.stt_limiter.max_per_ip = 10_000
    for _ in range(FLOOD_ATTEMPTS):
        assert await observed_close(f"{server.url}/ws/stt") == CLOSE_UNAUTHORIZED
    with pytest.raises(InvalidStatus):
        async with websockets.connect(f"{server.url}/ws/stt"):
            pass
