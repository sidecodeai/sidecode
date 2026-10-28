"""A browser that stops reading, against the real server, the real socket link and a real call.

uvicorn's ``send`` waits for the socket to become writable, so a peer that stops draining its end
holds every send, including the ``close``, for as long as it likes. The gateway must still end the
call, close the Live API's session and let go of the call's slot. The client here is a raw socket
that completes the WebSocket handshake and then never reads again.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
import uvicorn
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket

from rc_gateway.live import call as call_module
from rc_gateway.live.browser import WebSocketLink
from rc_gateway.live.call import LiveCall
from rc_gateway.live.config import LiveConfig
from rc_gateway.live.drafts import DraftBox
from rc_gateway.live.errors import LiveError
from rc_gateway.live.gate import ConfirmGate
from rc_gateway.live.playback import OutputPacer, Playback
from rc_gateway.live.tools import ToolRunner

from .fakes import FakeUpstream, pcm_ms

pytestmark = pytest.mark.timeout(60)

App = Callable[[Scope, Receive, Send], Awaitable[None]]


def non_reading_peer(port: int) -> socket.socket:
    """Complete the handshake, then never read a byte again."""
    peer = socket.socket()
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    peer.connect(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    peer.sendall(
        (
            "GET /ws/live HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    peer.recv(1024)  # the 101 response
    return peer


class Server:
    """The gateway's own server settings around one ASGI app, on a loop of its own."""

    def __init__(self, app: App) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            ws_ping_interval=None,
            ws_ping_timeout=None,
            ws_max_size=72 * 1024 * 1024,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve()), daemon=True
        )
        self._peers: list[socket.socket] = []

    def __enter__(self) -> Server:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise AssertionError("the server did not start")

    def peer(self) -> socket.socket:
        """A client that completes the handshake and never reads again."""
        peer = non_reading_peer(self.port)
        self._peers.append(peer)
        return peer

    def __exit__(self, *_: object) -> None:
        # The server waits for its connections on the way down, so the peers go first.
        for peer in self._peers:
            peer.close()
        self._server.should_exit = True
        self._thread.join(10)


def wait_for(done: threading.Event, seconds: float) -> None:
    assert done.wait(seconds), "the server side never finished"


def test_ending_a_link_to_a_peer_that_stopped_reading_returns_in_bounded_time() -> None:
    result: dict[str, Any] = {}
    finished = threading.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            return
        ws = WebSocket(scope, receive, send)
        await ws.accept()
        link = WebSocketLink(ws, send_timeout=0.3, farewell_timeout=0.3, close_timeout=0.3)
        chunk = b"\x01" * 32768
        for _ in range(8000):
            try:
                await link.send_bytes(chunk)
            except LiveError as exc:
                result["stalled"] = exc.code
                break
        started = time.monotonic()
        await link.end("hangup")
        result["end_seconds"] = time.monotonic() - started
        finished.set()

    with Server(app) as server:
        server.peer()
        wait_for(finished, 40)
    assert result["stalled"] == "browser_stalled"
    # The farewell and the close are each given 0.3 s; nothing about the peer can stretch that.
    assert result["end_seconds"] < 3.0


def test_a_call_over_a_browser_that_stopped_reading_closes_the_service_and_frees_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The assistant's audio is handed to the browser as fast as the socket takes it, so the peer's
    # not reading is felt within a moment instead of at the speed of speech.
    monkeypatch.setattr(
        call_module,
        "Playback",
        lambda send, flush, clock: Playback(
            send, flush, clock=clock, pacer=OutputPacer(lead=1e9, max_held_seconds=60)
        ),
    )
    result: dict[str, Any] = {}
    finished = threading.Event()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket":
            return
        ws = WebSocket(scope, receive, send)
        await ws.accept()
        upstream = FakeUpstream()
        slots: list[str] = []

        async def on_end(call: LiveCall) -> None:
            slots.append("released")

        call = LiveCall(
            username="admin",
            browser=WebSocketLink(ws, send_timeout=0.5, farewell_timeout=0.3, close_timeout=0.3),
            upstream=upstream,
            started={"session": {"id": "sess"}},
            config=LiveConfig(api_key="k", idle_seconds=10_000, max_call_seconds=10_000),
            tools=cast(ToolRunner, SimpleNamespace()),
            drafts=DraftBox(),
            gate=ConfirmGate(),
            on_end=on_end,
            close_grace=0.5,
            step_timeout=3.0,
        )
        task = asyncio.create_task(call.run())
        loud = base64.b64encode(pcm_ms(20_000)).decode()
        for _ in range(400):
            if call.end_reason is not None:
                break
            upstream.push({"type": "session.output_audio.delta", "delta": loud})
            await asyncio.sleep(0.01)
        stalled_at = time.monotonic()
        await asyncio.wait_for(task, 20)
        result.update(
            reason=call.end_reason,
            upstream_closed=upstream.closed,
            asked_to_close="session.close" in upstream.types(),
            slots=slots,
            seconds_to_finish=time.monotonic() - stalled_at,
        )
        finished.set()

    with Server(app) as server:
        server.peer()
        wait_for(finished, 40)
    assert result["reason"] == "browser_stalled"
    assert result["asked_to_close"] is True
    assert result["upstream_closed"] is True
    assert result["slots"] == ["released"]
    assert result["seconds_to_finish"] < 8.0
