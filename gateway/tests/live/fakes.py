"""Fakes for a call's two ends, and a real local WebSocket server standing in for the Live API."""

from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from rc_gateway.live.errors import LiveError

API_KEY = "sk-live-test-key-do-not-leak"
SAMPLE_RATE = 24_000
FRAME_BYTES = SAMPLE_RATE * 2 * 20 // 1000

#: 20 ms of PCM16 silence, and of a steady loud tone-like signal well above the voice threshold.
QUIET = b"\x00\x00" * (FRAME_BYTES // 2)
LOUD = struct.pack("<h", 4000) * (FRAME_BYTES // 2)


def pcm_ms(milliseconds: int, level: int = 1000) -> bytes:
    """Assistant-style audio: ``milliseconds`` of a constant sample."""
    return struct.pack("<h", level) * (SAMPLE_RATE * milliseconds // 1000)


class FakeClock:
    """A clock a test moves by hand, so limits can be crossed without waiting for them."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_LOST = object()


class FakeBrowser:
    """The browser's end of a call, in memory."""

    def __init__(self) -> None:
        self.audio: list[bytes] = []
        self.messages: list[dict[str, Any]] = []
        self.ended: str | None = None
        self.gone = False
        self.fail_sends: str | None = None
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def send_bytes(self, data: bytes) -> None:
        self._check()
        self.audio.append(data)

    async def send_json(self, message: dict[str, Any]) -> None:
        self._check()
        self.messages.append(message)

    def _check(self) -> None:
        if self.fail_sends is not None:
            raise LiveError(self.fail_sends)
        if self.gone:
            raise LiveError("browser_closed")

    async def receive(self) -> bytes | dict[str, Any] | None:
        item = await self._inbox.get()
        if isinstance(item, LiveError):
            raise item
        return item  # type: ignore[no-any-return]

    async def end(self, reason: str) -> None:
        self.ended = reason
        self.gone = True

    def mic(self, frame: bytes) -> None:
        self._inbox.put_nowait(frame)

    def command(self, **message: Any) -> None:
        self._inbox.put_nowait(message)

    def close(self) -> None:
        self._inbox.put_nowait(None)

    def types(self) -> list[str]:
        return [str(message.get("type")) for message in self.messages]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [message for message in self.messages if message.get("type") == kind]

    def audio_ms(self) -> float:
        return sum(len(chunk) for chunk in self.audio) / (SAMPLE_RATE * 2) * 1000


class FakeUpstream:
    """The Live API's end of a call, in memory. It answers ``session.close`` like the service."""

    def __init__(self, *, answer_close: bool = True, final_seconds: float = 42.0) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.dropped = False
        self.send_error: str | None = None
        self._answer_close = answer_close
        self._final_seconds = final_seconds
        self._inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, event: dict[str, Any]) -> None:
        if self.dropped:
            raise LiveError("upstream_lost")
        if self.send_error is not None:
            raise LiveError(self.send_error)
        self.sent.append(event)
        if event.get("type") == "session.close" and self._answer_close:
            self.push(
                {
                    "type": "session.closed",
                    "reason": "close_requested",
                    "usage": {"seconds": self._final_seconds},
                }
            )

    async def receive(self) -> dict[str, Any]:
        item = await self._inbox.get()
        if item is _LOST:
            raise LiveError("upstream_lost")
        return item  # type: ignore[no-any-return]

    async def close(self) -> None:
        self.closed = True

    def push(self, event: dict[str, Any]) -> None:
        self._inbox.put_nowait(event)

    async def settled(self) -> None:
        """Wait until the call has taken everything pushed so far and acted on it."""
        while not self._inbox.empty():
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.02)

    def drop(self) -> None:
        self.dropped = True
        self._inbox.put_nowait(_LOST)

    def types(self) -> list[str]:
        return [str(event.get("type")) for event in self.sent]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self.sent if event.get("type") == kind]


async def until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


class FakeLiveServer:
    """A Live API server on a loop of its own: a real WebSocket for the gateway to dial.

    It checks the key, answers ``session.start`` with ``session.started``, records every event the
    gateway sends, and answers ``session.close`` with ``session.closed``. A test makes it say more
    with ``push`` and makes it hang up with ``drop``.
    """

    def __init__(
        self,
        *,
        answer_close: bool = True,
        reject_status: int | None = None,
        final_seconds: float = 12.0,
        expires_in: float = 3600.0,
    ) -> None:
        self.answer_close = answer_close
        self.reject_status = reject_status
        self.final_seconds = final_seconds
        self.expires_in = expires_in
        self.received: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.path = ""
        self.port = 0
        self.connections = 0
        self.open_connections = 0
        self._connection: ServerConnection | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(5), "the fake Live API did not start"

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request
        ) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        self.path = request.path
        self.headers = {key.lower(): value for key, value in request.headers.items()}
        wanted = f"Bearer {API_KEY}"
        if self.reject_status is not None or self.headers.get("authorization") != wanted:
            status = HTTPStatus(self.reject_status or 401)
            return Response(status.value, status.phrase, Headers(), b"refused\n")
        return None

    async def _handle(self, connection: ServerConnection) -> None:
        self.connections += 1
        self.open_connections += 1
        self._connection = connection
        try:
            async for raw in connection:
                event = json.loads(raw)
                self.received.append(event)
                kind = event.get("type")
                if kind == "session.start":
                    await connection.send(json.dumps(self._started(event)))
                elif kind == "session.close" and self.answer_close:
                    await connection.send(json.dumps(self._closed("close_requested")))
                    await connection.close()
        finally:
            self.open_connections -= 1

    def _started(self, start: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "session.started",
            "event_id": "evt_started",
            "client_event_id": start.get("event_id"),
            "session": {
                **start.get("session", {}),
                "id": "live_sess_test",
                "status": "active",
                "expires_at": int(time.time() + self.expires_in),
            },
        }

    def _closed(self, reason: str) -> dict[str, Any]:
        return {
            "type": "session.closed",
            "event_id": "evt_closed",
            "reason": reason,
            "usage": {"seconds": self.final_seconds},
            "session": {"id": "live_sess_test", "status": "active"},
        }

    # ---- driven from a test ----

    def push(self, event: dict[str, Any]) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._send(event), self._loop).result(5)

    async def _send(self, event: dict[str, Any]) -> None:
        assert self._connection is not None
        await self._connection.send(json.dumps(event))

    def drop(self, code: int = 1011) -> None:
        assert self._loop is not None and self._connection is not None
        connection = self._connection
        asyncio.run_coroutine_threadsafe(connection.close(code), self._loop).result(5)

    def types(self) -> list[str]:
        return [str(event.get("type")) for event in list(self.received)]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in list(self.received) if event.get("type") == kind]

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("the fake Live API never saw what the test waited for")
