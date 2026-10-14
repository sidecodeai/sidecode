"""Bounded per-connection send queues.

Adapted from cc-remote's ``cc_remote/relay/forward.py`` (MIT, see THIRD_PARTY_NOTICES.md). A slow or
half-dead phone must never block a device or grow gateway memory without bound. Frames are
serialised before they are queued so both the item count and the queued byte count are hard limits,
and exceeding either drops the whole connection: shedding individual events would leave the app
believing it has a complete turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from .logging import logger

log = logger("rc_gateway.connections")

#: Apps receive a stream of bounded events (≤ 64 KiB each), so the item count is the real limit.
APP_QUEUE_CAP = 2048
APP_QUEUE_BYTES = 16 * 1024 * 1024
#: Devices receive requests, not streams: few of them, but one ``session.send`` with attachments
#: can be tens of megabytes, so the byte budget has to hold a maximal frame with headroom.
DEVICE_QUEUE_CAP = 256
DEVICE_QUEUE_BYTES = 96 * 1024 * 1024
QUEUE_CAP = APP_QUEUE_CAP
QUEUE_BYTES = APP_QUEUE_BYTES

Frame = dict[str, Any]


class SlowClientError(RuntimeError):
    """The peer cannot keep up with its bounded send queue."""


def encode(frame: Frame) -> str:
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Queued:
    """One serialised frame waiting for the socket, and what it holds until it is written."""

    raw: str
    #: What the string costs this process, not what it costs the wire: the queue caps exist to
    #: bound memory, and asking for the UTF-8 length would materialise a second copy of a payload
    #: that §5 allows to be tens of megabytes.
    size: int
    #: Called exactly once when the frame leaves the queue, however it leaves it. The forward
    #: path uses it to give back the in-flight budget it claimed.
    release: Callable[[], None] | None = None

    def done(self) -> None:
        if self.release is not None:
            self.release()


def queued_size(raw: str) -> int:
    """The memory one serialised frame occupies, in O(1)."""
    return sys.getsizeof(raw)


class Connection:
    """One live WebSocket with a bounded writer task."""

    def __init__(
        self,
        ws: WebSocket,
        *,
        cap: int = QUEUE_CAP,
        byte_cap: int = QUEUE_BYTES,
    ) -> None:
        self.ws = ws
        self.id = uuid.uuid4().hex
        self.cap = max(1, cap)
        self.byte_cap = max(1024, byte_cap)
        self.queue: asyncio.Queue[Queued] = asyncio.Queue(maxsize=self.cap)
        self.last_frame_at = time.monotonic()
        self.last_ping_at = time.monotonic()
        self.ping_sent_at: float | None = None
        self.latency_ms: int | None = None
        #: The code this side closed with, or ``None`` when the peer or the network ended it.
        #: Amendment A13 reads it to tell a revoked device from a link that merely dropped.
        self.close_code: int | None = None
        self._queued_bytes = 0
        self._sender: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or bool(self._sender and self._sender.done())

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    def start(self) -> None:
        self._sender = asyncio.create_task(self._run())

    def note_frame(self) -> None:
        self.last_frame_at = time.monotonic()

    def silent_for(self) -> float:
        return time.monotonic() - self.last_frame_at

    def note_ping_sent(self) -> None:
        now = time.monotonic()
        self.last_ping_at = now
        if self.ping_sent_at is None:
            self.ping_sent_at = now

    def note_pong(self) -> None:
        if self.ping_sent_at is not None:
            self.latency_ms = max(0, int((time.monotonic() - self.ping_sent_at) * 1000))
            self.ping_sent_at = None

    async def send(self, frame: Frame, *, release: Callable[[], None] | None = None) -> None:
        """Queue one complete frame or fail the whole slow connection.

        The frame is serialised once and the queued string is what the byte accounting measures:
        encoding it a second time to count its UTF-8 length would hold two copies of a payload
        that §5 allows to reach tens of megabytes. ``release`` passes to this connection, which
        calls it exactly once whether the frame is written, dropped or refused.
        """
        if self.closed:
            if release is not None:
                release()
            raise ConnectionError("connection sender is closed")
        raw = encode(frame)
        size = queued_size(raw)
        if self.queue.full() or self._queued_bytes + size > self.byte_cap:
            if release is not None:
                release()
            raise SlowClientError(
                f"send queue limit exceeded: items={self.queue.qsize()}/{self.cap} "
                f"bytes={self._queued_bytes + size}/{self.byte_cap}"
            )
        self.queue.put_nowait(Queued(raw, size, release))
        self._queued_bytes += size

    async def stop(self, *, code: int | None = None, reason: str = "") -> None:
        already_closed = self._closed
        self._closed = True
        if code is not None and self.close_code is None:
            self.close_code = code
        if code is not None and not already_closed:
            with contextlib.suppress(Exception):
                await self.ws.close(code=code, reason=reason[:123])
        sender = self._sender
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
        self.drain()

    def drain(self) -> None:
        """Throw away what the queue still holds, releasing every frame's budget with it."""
        while True:
            try:
                item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_bytes = max(0, self._queued_bytes - item.size)
            item.done()

    async def _run(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                self._queued_bytes = max(0, self._queued_bytes - item.size)
                try:
                    await self.ws.send_text(item.raw)
                finally:
                    item.done()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._closed = True
            log.debug("connection sender ended", connection=self.id, error=str(exc))
            with contextlib.suppress(Exception):
                await self.ws.close(code=1011, reason="sender failed")


class AppConnection(Connection):
    """An app WebSocket plus its per-session replay cursors."""

    def __init__(self, ws: WebSocket, username: str, **kwargs: int) -> None:
        kwargs.setdefault("cap", APP_QUEUE_CAP)
        kwargs.setdefault("byte_cap", APP_QUEUE_BYTES)
        super().__init__(ws, **kwargs)
        self.username = username
        self.cursors: dict[str, int] = {}
        self.last_active_at = time.monotonic()

    def note_activity(self) -> None:
        """Record real user traffic. Heartbeat ``pong`` frames deliberately do not count."""
        self.last_active_at = time.monotonic()

    def idle_for(self) -> float:
        return time.monotonic() - self.last_active_at

    def subscribe(self, session_id: str, cursor: int) -> None:
        self.cursors[session_id] = cursor

    def unsubscribe(self, session_id: str) -> None:
        self.cursors.pop(session_id, None)

    def subscribed(self, session_id: str) -> bool:
        return session_id in self.cursors


class DeviceConnection(Connection):
    """A device WebSocket. ``device_id`` is fixed by the token, never by the hello."""

    def __init__(self, ws: WebSocket, device_id: str, **kwargs: int) -> None:
        kwargs.setdefault("cap", DEVICE_QUEUE_CAP)
        kwargs.setdefault("byte_cap", DEVICE_QUEUE_BYTES)
        super().__init__(ws, **kwargs)
        self.device_id = device_id
        self.agents: list[dict[str, Any]] = []
        self.hello: Frame = {}
