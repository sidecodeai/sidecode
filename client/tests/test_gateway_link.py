"""The daemon loop driven against a fake gateway WebSocket server."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import websockets
from websockets.asyncio.client import connect as websockets_connect
from websockets.asyncio.server import ServerConnection, serve

from rc_client import gateway as rc_gateway
from rc_client import linkstate
from rc_client.errors import RcError
from rc_client.gateway import ByteQueue, GatewayLink, close_code, describe_error, reconnect_delay
from rc_client.proxy import DIRECT


class FakeGateway:
    """A minimal `/ws/device` endpoint that records what the device sends."""

    def __init__(self, *, reject_first: bool = False) -> None:
        self.received: list[dict[str, Any]] = []
        self.tokens: list[str | None] = []
        self.connections = 0
        self.reject_first = reject_first
        self.ready = asyncio.Event()
        self._server: Any = None
        self._sockets: list[ServerConnection] = []

    @property
    def url(self) -> str:
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/ws/device"

    async def start(self) -> None:
        self._server = await serve(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def close_current(self, code: int = 1000, reason: str = "") -> None:
        for socket in list(self._sockets):
            await socket.close(code, reason)

    async def send(self, frame: dict[str, Any]) -> None:
        await self._sockets[-1].send(json.dumps(frame))

    async def _handle(self, socket: ServerConnection) -> None:
        self.connections += 1
        self.tokens.append(socket.request.headers.get("Authorization") if socket.request else None)
        if self.reject_first and self.connections == 1:
            await socket.close()
            return
        self._sockets.append(socket)
        try:
            async for raw in socket:
                frame = json.loads(raw)
                self.received.append(frame)
                if frame.get("type") == "hello":
                    await socket.send(
                        json.dumps(
                            {
                                "type": "hello_ack",
                                "device_id": "dev-1",
                                "server_time": 0,
                                "config": {"delta_flush_ms": 80, "max_event_bytes": 65536},
                            }
                        )
                    )
                    self.ready.set()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            with contextlib.suppress(ValueError):
                self._sockets.remove(socket)

    def frames(self, frame_type: str) -> list[dict[str, Any]]:
        return [frame for frame in self.received if frame.get("type") == frame_type]

    async def wait_for(self, frame_type: str, timeout: float = 3.0) -> dict[str, Any]:
        async def poll() -> dict[str, Any]:
            while True:
                matching = self.frames(frame_type)
                if matching:
                    return matching[-1]
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=timeout)


@pytest.fixture
async def gateway() -> AsyncIterator[FakeGateway]:
    server = FakeGateway()
    await server.start()
    yield server
    await server.stop()


async def hello_payload() -> dict[str, Any]:
    return {
        "name": "test-device",
        "platform": "macos",
        "hostname": "test.local",
        "arch": "arm64",
        "agents": [],
        "sessions": [],
    }


async def link_to(
    server: FakeGateway, handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]
) -> GatewayLink:
    link = GatewayLink(
        server.url, "device-token", hello=hello_payload, handlers=handlers, proxy=DIRECT
    )
    link.start()
    await asyncio.wait_for(server.ready.wait(), timeout=5)

    async def settle() -> None:
        while not link.connected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(settle(), timeout=5)
    return link


async def test_hello_is_sent_with_the_bearer_token_and_protocol_version(
    gateway: FakeGateway,
) -> None:
    link = await link_to(gateway, {})
    hello = gateway.frames("hello")[0]
    assert hello["protocol"] == 1
    assert hello["name"] == "test-device"
    assert hello["client_version"]
    assert gateway.tokens[0] == "Bearer device-token"
    assert link.connected
    await link.stop()


async def test_a_forwarded_request_is_answered_with_a_reply(gateway: FakeGateway) -> None:
    async def handler(frame: dict[str, Any]) -> dict[str, Any]:
        return {"echo": frame.get("text")}

    link = await link_to(gateway, {"session.send": handler})
    await gateway.send({"type": "session.send", "id": "req-1", "from": "app-1", "text": "hello"})
    reply = await gateway.wait_for("reply")
    assert reply == {
        "type": "reply",
        "id": "req-1",
        "from": "app-1",
        "ok": True,
        "result": {"echo": "hello"},
    }
    await link.stop()


async def test_handler_errors_become_protocol_error_replies(gateway: FakeGateway) -> None:
    async def handler(frame: dict[str, Any]) -> dict[str, Any]:
        raise RcError("conflict", "controlled by terminal; take over first")

    link = await link_to(gateway, {"session.send": handler})
    await gateway.send({"type": "session.send", "id": "req-2", "from": "app-1"})
    reply = await gateway.wait_for("reply")
    assert reply["ok"] is False
    assert reply["error"] == {
        "code": "conflict",
        "message": "controlled by terminal; take over first",
    }
    await link.stop()


async def test_an_unexpected_handler_failure_still_replies(gateway: FakeGateway) -> None:
    async def handler(frame: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("boom")

    link = await link_to(gateway, {"session.stop": handler})
    await gateway.send({"type": "session.stop", "id": "req-3", "from": "app-1"})
    reply = await gateway.wait_for("reply")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "internal"
    await link.stop()


async def test_unknown_request_types_are_refused_as_unsupported(gateway: FakeGateway) -> None:
    link = await link_to(gateway, {})
    await gateway.send({"type": "session.teleport", "id": "req-4", "from": "app-1"})
    reply = await gateway.wait_for("reply")
    assert reply["error"]["code"] == "unsupported"
    await link.stop()


async def test_ping_is_answered_with_pong(gateway: FakeGateway) -> None:
    link = await link_to(gateway, {})
    await gateway.send({"type": "ping"})
    await gateway.wait_for("pong")
    await link.stop()


async def test_the_link_reconnects_and_re_sends_hello(gateway: FakeGateway) -> None:
    link = await link_to(gateway, {})
    gateway.ready.clear()
    await gateway.close_current()
    await asyncio.wait_for(gateway.ready.wait(), timeout=8)
    assert gateway.connections >= 2
    assert len(gateway.frames("hello")) >= 2
    await link.stop()


async def test_frames_are_dropped_while_disconnected() -> None:
    link = GatewayLink(
        "ws://127.0.0.1:1/ws/device", "t", hello=hello_payload, handlers={}, proxy=DIRECT
    )
    await link.send({"type": "session.updated", "session": {}})
    await link.stop()


def test_byte_queue_bounds_items_and_bytes() -> None:
    queue = ByteQueue(max_items=2, max_bytes=1024)
    assert queue.offer("a")
    assert queue.offer("b")
    assert not queue.offer("c")
    assert len(queue) == 2

    tight = ByteQueue(max_items=10, max_bytes=2048)
    assert tight.offer("x" * 2000)
    assert not tight.offer("y" * 100)
    assert tight.clear() == 1
    assert tight.byte_size == 0


async def test_a_silent_gateway_is_detected_and_the_link_reconnects(
    gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half-open watchdog must interrupt a receiver blocked on a dead socket.

    The timeout is shortened rather than waited out; the production values are
    asserted separately.
    """
    monkeypatch.setattr(rc_gateway, "SILENCE_TIMEOUT", 0.3)
    monkeypatch.setattr(rc_gateway, "WATCHDOG_TICK", 0.05)
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (0.05,))
    link = await link_to(gateway, {})
    gateway.ready.clear()

    # The server never sends another frame and never closes: exactly the
    # half-open case a NAT produces after a network change.
    await asyncio.wait_for(gateway.ready.wait(), timeout=10)
    assert gateway.connections >= 2
    assert len(gateway.frames("hello")) >= 2
    await link.stop()


async def test_the_backoff_only_resets_after_a_stable_connection(
    gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway that accepts and immediately drops us must not be hammered."""
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (0.05, 0.1, 0.2))
    monkeypatch.setattr(rc_gateway, "STABLE_CONNECTION_SECONDS", 3600.0)
    link = await link_to(gateway, {})
    for _ in range(3):
        gateway.ready.clear()
        await gateway.close_current()
        await asyncio.wait_for(gateway.ready.wait(), timeout=10)
    assert gateway.connections >= 4
    await link.stop()


async def test_the_link_is_dialled_directly_and_ignores_configured_proxies(
    gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A system or environment SOCKS proxy must never reach the gateway link."""
    seen: list[Any] = []

    def spy(url: str, **kwargs: Any) -> Any:
        seen.append(kwargs.get("proxy", "missing"))
        # `rc_gateway.connect` is this function; naming it here rather than
        # reading it back off the module keeps the spy honest under monkeypatch.
        return websockets_connect(url, **kwargs)

    monkeypatch.setattr(rc_gateway, "connect", spy)
    link = await link_to(gateway, {})
    assert seen == [None]
    await link.stop()


def test_a_lost_link_is_logged_with_the_exception_message() -> None:
    reason = describe_error(
        ImportError("connecting through a SOCKS proxy requires python-socks"), "secret-token"
    )
    assert reason == "ImportError: connecting through a SOCKS proxy requires python-socks"


def test_the_logged_reason_hides_the_token_and_is_bounded() -> None:
    masked = describe_error(RuntimeError("rejected token=secret-token"), "secret-token")
    assert "secret-token" not in masked
    assert "***" in masked

    long = describe_error(RuntimeError("x" * 500))
    assert len(long) == rc_gateway.MAX_ERROR_CHARS + 3
    assert long.endswith("...")

    assert describe_error(RuntimeError()) == "RuntimeError"


# ------------------------------------------- what the link does on connect


async def test_the_pump_answers_requests_while_connect_work_is_still_running(
    gateway: FakeGateway,
) -> None:
    """A backfill that waits for the gateway cannot be what the gateway waits for."""
    released = asyncio.Event()
    started = asyncio.Event()

    async def on_ready() -> None:
        started.set()
        await released.wait()

    async def handler(frame: dict[str, Any]) -> dict[str, Any]:
        return {"echo": frame.get("text")}

    link = GatewayLink(
        gateway.url,
        "device-token",
        hello=hello_payload,
        handlers={"session.send": handler},
        on_ready=on_ready,
        proxy=DIRECT,
    )
    link.start()
    await asyncio.wait_for(gateway.ready.wait(), timeout=5)
    await asyncio.wait_for(started.wait(), timeout=5)

    await gateway.send({"type": "session.send", "id": "req-1", "from": "app-1", "text": "hi"})
    reply = await gateway.wait_for("reply")
    assert reply["result"] == {"echo": "hi"}
    released.set()
    await link.stop()


async def test_connect_work_is_dropped_when_the_connection_goes(
    gateway: FakeGateway,
) -> None:
    running = asyncio.Event()
    cancelled = asyncio.Event()

    async def on_ready() -> None:
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    link = GatewayLink(
        gateway.url,
        "device-token",
        hello=hello_payload,
        handlers={},
        on_ready=on_ready,
        proxy=DIRECT,
    )
    link.start()
    await asyncio.wait_for(running.wait(), timeout=5)
    await gateway.close_current()
    await asyncio.wait_for(cancelled.wait(), timeout=5)
    await link.stop()


# -------------------------------------------------- close codes (2.5 / A4)


def test_the_close_code_decides_whether_to_dial_again() -> None:
    assert 4401 in rc_gateway.FATAL_CLOSE_CODES
    assert 4403 in rc_gateway.FATAL_CLOSE_CODES
    assert 1011 not in rc_gateway.FATAL_CLOSE_CODES
    # A replaced connection waits rather than racing the one that replaced it.
    assert reconnect_delay(0, rc_gateway.REPLACED_CLOSE_CODE) >= rc_gateway.REPLACED_DELAY
    assert reconnect_delay(0, 1011) == rc_gateway.BACKOFF_STEPS[0]
    assert reconnect_delay(99, None) == rc_gateway.BACKOFF_STEPS[-1]


def test_a_close_code_is_read_from_the_exception() -> None:
    closed = websockets.exceptions.ConnectionClosedError(
        websockets.frames.Close(4401, "device revoked"), None
    )
    assert close_code(closed) == 4401
    assert close_code(RcError("timeout", "no frames")) is None


@pytest.mark.parametrize("code", [4401, 4403])
async def test_a_rejected_credential_stops_the_link(gateway: FakeGateway, code: int) -> None:
    link = await link_to(gateway, {})
    await gateway.close_current(code, "no")

    async def settle() -> None:
        while link._task is not None and not link._task.done():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(settle(), timeout=5)
    assert gateway.connections == 1, "the link never dialled again"
    state = linkstate.read()
    assert state is not None and state.status == linkstate.REJECTED
    await link.stop()


async def test_a_transient_close_is_retried(
    gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (0.01,))
    link = await link_to(gateway, {})
    await gateway.close_current(1011, "keepalive")

    async def reconnected() -> None:
        while gateway.connections < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(reconnected(), timeout=5)
    await link.stop()
