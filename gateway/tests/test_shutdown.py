"""The gateway still shuts down when an app's socket has stopped reading.

uvicorn waits for open connections with no limit unless it is given one. A WebSocket whose peer
has stopped reading (a phone that lost its network mid-stream, a browser tab the OS froze) has a
full send buffer: the close frame queues behind data nobody takes, the transport never closes, and
the wait never ends, so the application's own shutdown, which flushes the session index, is never
reached. `server_config` bounds the wait. The first test pins the number. The second runs a real
server against a real socket that never reads, with the bound shortened so the suite does not sit
out five seconds.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import uvicorn

from rc_gateway.__main__ import server_config
from rc_gateway.accounts import ADMIN_USERNAME
from rc_gateway.app import create_app
from rc_gateway.auth import make_session_token, session_token_claims
from rc_gateway.state import GatewayState

from .conftest import make_config

#: A frame under the 64 KiB an event may carry, repeated until the socket's buffers are full.
FILLER = {"type": "filler", "pad": "x" * 60_000}
#: Far more than any loopback buffer holds, and under the 16 MiB an app's send queue may keep.
MAX_FILLER_FRAMES = 200
#: The wait the second test gives uvicorn, in place of GRACEFUL_SHUTDOWN_SECONDS.
SHORT_WAIT_SECONDS = 1
#: The wait plus everything the shutdown does after it, with room for a slow machine.
SHUTDOWN_BOUND_SECONDS = SHORT_WAIT_SECONDS + 5


def test_the_server_stops_waiting_for_open_connections_after_five_seconds(tmp_path: Path) -> None:
    assert server_config(make_config(tmp_path)).timeout_graceful_shutdown == 5


async def test_a_client_that_never_reads_cannot_hold_the_shutdown_open(
    state: GatewayState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.__main__.GRACEFUL_SHUTDOWN_SECONDS", SHORT_WAIT_SECONDS)
    config = server_config(dataclasses.replace(state.config, port=0))
    # The real server settings around this test's own state, so the test can reach the hub.
    config.app, config.factory = create_app(state), False
    server = uvicorn.Server(config)

    cleaned_up = asyncio.Event()
    close_index = state.index.close

    async def close_index_and_note_it() -> None:
        await close_index()
        cleaned_up.set()

    monkeypatch.setattr(state.index, "close", close_index_and_note_it)

    serving = asyncio.create_task(server.serve())
    peer: socket.socket | None = None
    try:
        await _until(lambda: server.started)
        peer = await _open_app_socket_that_never_reads(server, state)
        await _fill_until_the_sender_is_blocked(state)

        server.should_exit = True
        finished, _ = await asyncio.wait({serving}, timeout=SHUTDOWN_BOUND_SECONDS)
        assert finished, f"the shutdown was still waiting after {SHUTDOWN_BOUND_SECONDS} s"
        assert cleaned_up.is_set(), "the application's shutdown did not reach the session index"
    finally:
        # A shutdown that never ends must not outlive the test.
        server.force_exit = True
        if peer is not None:
            peer.close()
        await serving


async def _until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 4.0
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def _open_app_socket_that_never_reads(
    server: uvicorn.Server, state: GatewayState
) -> socket.socket:
    """Connect to `/ws/app` by hand and leave everything the gateway sends unread."""
    token, _ = make_session_token(state.config.secret, 3600)
    claims = session_token_claims(token, state.config.secret)
    assert claims is not None
    await state.sessions.register(claims)

    port = int(server.servers[0].sockets[0].getsockname()[1])
    loop = asyncio.get_running_loop()
    peer = socket.socket()
    peer.setblocking(False)
    # A small receive window, so the gateway's side backs up after a few frames, not a few MiB.
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    await loop.sock_connect(peer, ("127.0.0.1", port))
    await loop.sock_sendall(peer, _upgrade_request(port, token))
    await _until(lambda: state.hub.app_count == 1)
    return peer


def _upgrade_request(port: int, token: str) -> bytes:
    key = base64.b64encode(os.urandom(16)).decode()
    return (
        "GET /ws/app HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n"
        "\r\n"
    ).encode()


async def _fill_until_the_sender_is_blocked(state: GatewayState) -> None:
    """Send until frames wait in the connection's queue, which only a full socket explains."""
    (connection,) = state.hub._apps.values()
    for _ in range(MAX_FILLER_FRAMES):
        await state.hub.broadcast_user(ADMIN_USERNAME, FILLER)
        await asyncio.sleep(0.01)
        if connection.queued_bytes:
            break
    else:
        raise AssertionError("the socket took every frame, so nothing was left unread")
    await asyncio.sleep(0.1)
    assert connection.queued_bytes, "the sender was not blocked on the unread socket"
