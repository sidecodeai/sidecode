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

    def __init__(
        self, *, reject_first: bool = False, before_ack: list[Any] | None = None, ack: bool = True
    ) -> None:
        # Frames sent after the hello and ahead of its acknowledgement, as raw text or as objects,
        # and whether the acknowledgement comes at all.
        self.before_ack = before_ack or []
        self.ack = ack
        # How many of the next connections are closed the moment they open, as a gateway that is
        # still starting up does.
        self.drop_next = 0
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
        if self.drop_next > 0:
            self.drop_next -= 1
            await socket.close()
            return
        self._sockets.append(socket)
        try:
            async for raw in socket:
                frame = json.loads(raw)
                self.received.append(frame)
                if frame.get("type") == "hello":
                    for early in self.before_ack:
                        await socket.send(early if isinstance(early, str) else json.dumps(early))
                    if not self.ack:
                        continue
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


async def test_a_request_that_reaches_the_device_before_the_ack_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway can make a device reachable before it acknowledges the hello, so an app's request
    can arrive first. It is served over the same connection, not answered by hanging up."""
    gateway = FakeGateway(
        before_ack=[{"type": "session.history", "id": "r-1", "from": "app-1", "session_id": "s-1"}]
    )
    await gateway.start()
    served_while_connected: list[bool] = []

    async def history(frame: dict[str, Any]) -> dict[str, Any]:
        served_while_connected.append(link.connected)
        return {"events": [], "has_more": False}

    link = GatewayLink(
        gateway.url,
        "device-token",
        hello=hello_payload,
        handlers={"session.history": history},
        proxy=DIRECT,
    )
    link.start()
    reply = await gateway.wait_for("reply")

    assert reply == {
        "type": "reply",
        "id": "r-1",
        "from": "app-1",
        "ok": True,
        "result": {"events": [], "has_more": False},
    }
    assert gateway.connections == 1
    assert served_while_connected == [True], "it is served once the ack is in, not before"
    await link.stop()
    await gateway.stop()


async def test_requests_that_come_before_the_ack_are_served_in_the_order_they_came() -> None:
    gateway = FakeGateway(
        before_ack=[
            {"type": "session.send", "id": "r-1", "from": "app-1", "text": "one"},
            {"type": "ping"},
            {"type": "session.send", "id": "r-2", "from": "app-2", "text": "two"},
        ]
    )
    await gateway.start()

    async def send(frame: dict[str, Any]) -> dict[str, Any]:
        return {"echo": frame["text"]}

    link = await link_to(gateway, {"session.send": send})

    async def answered() -> None:
        while len(gateway.frames("reply")) < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(answered(), timeout=5)
    replies = gateway.frames("reply")
    assert [(reply["id"], reply["result"]) for reply in replies] == [
        ("r-1", {"echo": "one"}),
        ("r-2", {"echo": "two"}),
    ]
    assert len(gateway.frames("pong")) == 1
    assert gateway.connections == 1
    await link.stop()
    await gateway.stop()


async def test_a_request_of_a_kind_the_device_does_not_know_is_refused_not_hung_up_on() -> None:
    gateway = FakeGateway(before_ack=[{"type": "session.teleport", "id": "r-3", "from": "app-3"}])
    await gateway.start()

    link = await link_to(gateway, {})
    reply = await gateway.wait_for("reply")

    assert reply["id"] == "r-3"
    assert reply["error"]["code"] == "unsupported"
    assert gateway.connections == 1
    await link.stop()
    await gateway.stop()


async def test_a_frame_that_is_not_a_frame_before_the_ack_does_not_end_the_link() -> None:
    gateway = FakeGateway(before_ack=["not json at all", "[1, 2]", '"text"'])
    await gateway.start()

    link = await link_to(gateway, {})

    assert link.connected
    assert gateway.connections == 1
    await link.stop()
    await gateway.stop()


async def test_a_gateway_that_sends_more_than_a_few_frames_before_the_ack_is_hung_up_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """What waits for the ack is kept in memory, so it is bounded; the link goes, and comes back."""
    monkeypatch.setattr(rc_gateway, "MAX_EARLY_FRAMES", 3)
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (0.05,))
    gateway = FakeGateway(before_ack=[{"type": "ping"}] * 4)
    await gateway.start()
    link = GatewayLink(gateway.url, "device-token", hello=hello_payload, handlers={}, proxy=DIRECT)
    link.start()

    async def redialled() -> None:
        while gateway.connections < 3:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(redialled(), timeout=5)

    assert not link.connected, "no connection of that kind is ever served"
    assert gateway.frames("pong") == []
    assert "too many frames before hello_ack" in [record.getMessage() for record in caplog.records]
    await link.stop()
    await gateway.stop()


@pytest.mark.parametrize("frames", [2, 1])
async def test_a_gateway_that_sends_more_bytes_than_the_cap_before_the_ack_is_hung_up_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, frames: int
) -> None:
    """The count alone leaves room for 256 frames of 80 MiB: what waits is bounded in bytes too."""
    monkeypatch.setattr(rc_gateway, "MAX_EARLY_BYTES", 1000)
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (0.05,))
    size = 600 if frames == 2 else 2000
    gateway = FakeGateway(before_ack=[{"type": "ping", "padding": "x" * size}] * frames)
    await gateway.start()
    link = GatewayLink(gateway.url, "device-token", hello=hello_payload, handlers={}, proxy=DIRECT)
    link.start()

    async def redialled() -> None:
        while gateway.connections < 3:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(redialled(), timeout=5)

    assert not link.connected
    assert gateway.frames("pong") == []
    assert "too much data before hello_ack" in [record.getMessage() for record in caplog.records]
    await link.stop()
    await gateway.stop()


async def test_a_gateway_that_stays_under_the_byte_cap_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rc_gateway, "MAX_EARLY_BYTES", 1000)
    gateway = FakeGateway(before_ack=[{"type": "ping", "padding": "x" * 300}] * 2)
    await gateway.start()

    link = await link_to(gateway, {})

    assert link.connected and gateway.connections == 1
    assert len(gateway.frames("pong")) == 2
    await link.stop()
    await gateway.stop()


async def test_a_gateway_that_sends_as_many_frames_as_the_cap_allows_is_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rc_gateway, "MAX_EARLY_FRAMES", 3)
    gateway = FakeGateway(before_ack=[{"type": "ping"}] * 3)
    await gateway.start()

    link = await link_to(gateway, {})

    assert link.connected and gateway.connections == 1
    assert len(gateway.frames("pong")) == 3
    await link.stop()
    await gateway.stop()


async def test_a_gateway_that_never_acknowledges_the_hello_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rc_gateway, "ACK_TIMEOUT", 0.2)
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (0.05,))
    gateway = FakeGateway(ack=False, before_ack=[{"type": "ping"}])
    await gateway.start()
    link = GatewayLink(gateway.url, "device-token", hello=hello_payload, handlers={}, proxy=DIRECT)
    link.start()

    async def redialled() -> None:
        while gateway.connections < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(redialled(), timeout=5)
    assert not link.connected
    assert len(gateway.frames("hello")) >= 2
    await link.stop()
    await gateway.stop()


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


# ------------------------------------------- a gateway that restarts (1012)


def test_a_restarting_gateway_is_asked_again_about_every_second_for_half_a_minute() -> None:
    backoff = rc_gateway.Backoff()
    delays = [backoff.next(1012, stayed_up=True, now=100.0)]
    for second in range(1, 30):
        delays.append(backoff.next(None, stayed_up=False, now=100.0 + second))

    low, high = 1.0 - rc_gateway.RESTART_JITTER, 1.0 + rc_gateway.RESTART_JITTER
    assert all(low <= delay <= high for delay in delays)
    assert len(set(delays)) > 1, (
        "a little unevenly, so devices cut off together do not arrive together"
    )


def test_after_the_half_minute_the_usual_backoff_goes_on_from_where_it_was() -> None:
    backoff = rc_gateway.Backoff()
    steps = rc_gateway.BACKOFF_STEPS
    assert backoff.next(None, stayed_up=False, now=0.0) == steps[0]
    assert backoff.next(None, stayed_up=False, now=1.0) == steps[1]

    backoff.next(1012, stayed_up=False, now=10.0)
    for second in range(11, 40):
        assert backoff.next(None, stayed_up=False, now=float(second)) < 1.2

    assert backoff.next(None, stayed_up=False, now=40.5) == steps[2]
    assert backoff.next(None, stayed_up=False, now=45.0) == steps[3]


def test_a_connection_that_stayed_up_before_the_restart_starts_the_backoff_over() -> None:
    backoff = rc_gateway.Backoff()
    for second in range(4):
        backoff.next(None, stayed_up=False, now=float(second))

    backoff.next(1012, stayed_up=True, now=100.0)

    assert backoff.next(None, stayed_up=False, now=131.0) == rc_gateway.BACKOFF_STEPS[0]


def test_a_second_restart_extends_the_half_minute() -> None:
    backoff = rc_gateway.Backoff()
    backoff.next(1012, stayed_up=True, now=0.0)
    backoff.next(1012, stayed_up=False, now=25.0)

    assert backoff.next(None, stayed_up=False, now=50.0) < 1.2
    assert backoff.next(None, stayed_up=False, now=56.0) == rc_gateway.BACKOFF_STEPS[0]


def test_a_connection_that_another_one_replaced_waits_even_in_the_half_minute() -> None:
    backoff = rc_gateway.Backoff()
    backoff.next(1012, stayed_up=True, now=0.0)

    assert backoff.next(rc_gateway.REPLACED_CLOSE_CODE, stayed_up=False, now=5.0) >= (
        rc_gateway.REPLACED_DELAY
    )


def test_a_gateway_that_closes_for_any_other_reason_gets_the_usual_backoff() -> None:
    backoff = rc_gateway.Backoff()
    assert backoff.next(1011, stayed_up=False, now=0.0) == rc_gateway.BACKOFF_STEPS[0]
    assert backoff.next(1011, stayed_up=False, now=1.0) == rc_gateway.BACKOFF_STEPS[1]
    assert backoff.next(None, stayed_up=False, now=2.0) == rc_gateway.BACKOFF_STEPS[2]


async def test_a_link_cut_by_a_restart_keeps_asking_while_the_gateway_comes_up(
    gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the window the second try waits two seconds, and the third four."""
    monkeypatch.setattr(rc_gateway, "BACKOFF_STEPS", (2.0, 4.0, 8.0))
    monkeypatch.setattr(rc_gateway, "RESTART_RETRY", 0.05)
    monkeypatch.setattr(rc_gateway, "RESTART_WINDOW", 5.0)
    link = await link_to(gateway, {})
    gateway.ready.clear()
    gateway.drop_next = 3

    await gateway.close_current(1012, "service restart")

    async def back() -> None:
        await gateway.ready.wait()
        while not link.connected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(back(), timeout=1.5)

    assert gateway.connections == 5
    await link.stop()


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
