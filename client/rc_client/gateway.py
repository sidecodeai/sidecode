"""The outbound WebSocket link to the gateway.

The device never listens: it dials out, sends `hello`, and answers forwarded
requests. Because mobile NAT rarely delivers a close frame, silence for 60 s is
treated as a dead connection and forces a reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from . import PROTOCOL_VERSION, __version__, linkstate
from .errors import RcError
from .logging_setup import logger
from .proxy import websocket_proxy

log = logger("rc_client.gateway")

BACKOFF_STEPS = (1.0, 2.0, 4.0, 8.0, 15.0)
SILENCE_TIMEOUT = 60.0
# How long the gateway has to acknowledge a hello, and how many frames, and how many bytes in all,
# it may send ahead of the ack: the requests it held for a device that was away, and nothing like
# this many.
ACK_TIMEOUT = 30.0
MAX_EARLY_FRAMES = 256
MAX_EARLY_BYTES = 8 * 1024 * 1024
WATCHDOG_TICK = 5.0
STABLE_CONNECTION_SECONDS = 30.0
# PROTOCOL.md 2.5: the close code says whether reconnecting is worth trying.
# These two are the credential's answer, and retrying only hammers the gateway
# with something it has already refused.
REJECTED = "credential rejected; re-enroll with `rc-client enroll`"
FATAL_CLOSE_CODES = {
    4401: REJECTED,
    4403: "this device is not allowed on this gateway; re-enroll with `rc-client enroll`",
}
# A newer connection replaced this one. Racing it back would replace that one in
# turn, so this reconnect waits.
REPLACED_CLOSE_CODE = 4001
REPLACED_DELAY = 15.0
# The gateway closes with 1012 when it restarts, and it is back within seconds. For the half minute
# after one, the device asks again every second, a little unevenly so that the devices it cut off
# together do not all arrive together.
RESTART_CLOSE_CODE = 1012
RESTART_RETRY = 1.0
RESTART_JITTER = 0.15
RESTART_WINDOW = 30.0
# PROTOCOL §5 allows 8 attachments of 6 MiB decoded in one `session.send`, which
# is ~64 MiB of base64 plus the surrounding JSON.
MAX_FRAME_BYTES = 80 * 1024 * 1024
SEND_QUEUE_ITEMS = 4096
SEND_QUEUE_BYTES = 96 * 1024 * 1024
MAX_ERROR_CHARS = 200

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
# A gateway → device frame that is told, not asked: it carries no `id` and is
# never replied to. `preferences` is the first of them (amendment A35).
Signal = Callable[[dict[str, Any]], Awaitable[None]]
HelloBuilder = Callable[[], Awaitable[dict[str, Any]]]


def describe_error(exc: BaseException, secret: str = "") -> str:
    """`Class: message` for a log line, truncated and with the token masked.

    The class name alone hides why a link failed (a missing optional
    dependency and a refused TLS handshake both read as one word), so the
    message is kept, bounded in length and scrubbed of the device token.
    """
    message = str(exc).strip()
    if secret:
        message = message.replace(secret, "***")
    if not message:
        return type(exc).__name__
    text = f"{type(exc).__name__}: {message}"
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS] + "..."
    return text


def reconnect_delay(attempt: int, code: int | None) -> float:
    """How long to wait before dialling again (PROTOCOL.md 2.5)."""
    delay = BACKOFF_STEPS[min(max(attempt, 0), len(BACKOFF_STEPS) - 1)]
    if code == REPLACED_CLOSE_CODE:
        return max(delay, REPLACED_DELAY)
    return delay


def restart_delay() -> float:
    """How long to wait before asking a restarting gateway again."""
    return RESTART_RETRY * random.uniform(1 - RESTART_JITTER, 1 + RESTART_JITTER)


class Backoff:
    """How long to wait before dialling the gateway again.

    The usual backoff steps up with every failure, and starts over after a connection that stayed
    up. A gateway that closed with 1012 is restarting, so for `RESTART_WINDOW` after it the device
    asks again every second instead. Those tries do not count against the backoff, which goes on
    from where it was when the window is over. A connection another one replaced still waits.
    """

    def __init__(self) -> None:
        self._attempt = 0
        self._restarting_until = 0.0

    def next(self, code: int | None, stayed_up: bool, now: float) -> float:
        if stayed_up:
            self._attempt = 0
        if code == RESTART_CLOSE_CODE:
            self._restarting_until = now + RESTART_WINDOW
        if now < self._restarting_until and code != REPLACED_CLOSE_CODE:
            return restart_delay()
        delay = reconnect_delay(self._attempt, code)
        self._attempt += 1
        return delay


def close_code(exc: BaseException) -> int | None:
    """The code the gateway closed with, when it closed rather than vanished."""
    received = getattr(exc, "rcvd", None)
    code = getattr(received, "code", None)
    return int(code) if isinstance(code, int) else None


class ByteQueue:
    """FIFO with both an item cap and a serialized-byte cap."""

    def __init__(self, max_items: int, max_bytes: int) -> None:
        self._max_items = max(1, max_items)
        self._max_bytes = max(1024, max_bytes)
        self._items: list[tuple[str, int]] = []
        self._bytes = 0
        self._event = asyncio.Event()

    @property
    def byte_size(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._items)

    def offer(self, payload: str) -> bool:
        """Enqueue unless full. Returns False when the frame was dropped."""
        size = len(payload.encode("utf-8"))
        if size > self._max_bytes:
            return False
        if len(self._items) >= self._max_items or self._bytes + size > self._max_bytes:
            return False
        self._items.append((payload, size))
        self._bytes += size
        self._event.set()
        return True

    async def get(self) -> str:
        while not self._items:
            self._event.clear()
            await self._event.wait()
        payload, size = self._items.pop(0)
        self._bytes = max(0, self._bytes - size)
        return payload

    def clear(self) -> int:
        dropped = len(self._items)
        self._items.clear()
        self._bytes = 0
        return dropped


class GatewayLink:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        hello: HelloBuilder,
        handlers: dict[str, Handler],
        signals: dict[str, Signal] | None = None,
        on_ready: Callable[[], Awaitable[None]] | None = None,
        proxy: str,
    ) -> None:
        self.url = url
        self._token = token
        self._proxy = proxy
        self._hello = hello
        self._handlers = handlers
        self._signals = signals or {}
        self._on_ready = on_ready
        self._queue = ByteQueue(SEND_QUEUE_ITEMS, SEND_QUEUE_BYTES)
        self._connected = False
        # The hello's snapshot is taken and its ack has not come: see `send`.
        self._greeting = False
        self._stop = False
        self._last_frame = 0.0
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        for task in list(self._inflight):
            task.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def send(self, frame: dict[str, Any]) -> None:
        """Best-effort publish; drops while disconnected or when the queue is full.

        From the hello's snapshot until the gateway acknowledges it, a frame queues behind the
        hello instead: it describes a change the snapshot cannot hold, and the pump that sends it
        starts once the ack is in. Before the snapshot the frame is dropped, because the snapshot
        will hold what it describes.
        """
        if not (self._connected or self._greeting):
            return
        payload = json.dumps(frame, ensure_ascii=False)
        if not self._queue.offer(payload):
            log.warning("outbound frame dropped", type=frame.get("type"))

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        backoff = Backoff()
        while not self._stop:
            started = time.monotonic()
            code: int | None = None
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = close_code(exc)
                description = describe_error(exc, self._token)
                log.warning("gateway link lost", error=description)
                linkstate.record(linkstate.RETRYING, description)
            finally:
                self._connected = False
                self._greeting = False
                dropped = self._queue.clear()
                if dropped:
                    log.debug("dropped queued frames on disconnect", frames=dropped)
            if code in FATAL_CLOSE_CODES:
                reason = FATAL_CLOSE_CODES[code]
                log.error("gateway link stopped", code=code, reason=reason)
                linkstate.record(linkstate.REJECTED, f"close {code}")
                self._stop = True
                return
            # Only a connection that actually stayed up resets the backoff, so a
            # gateway that accepts and immediately drops us is not hammered.
            now = time.monotonic()
            await asyncio.sleep(backoff.next(code, now - started >= STABLE_CONNECTION_SECONDS, now))

    async def _session(self) -> None:
        # The link is a long-lived tunnel to the operator's own gateway, so it is
        # dialled directly unless the device was enrolled with a proxy: the
        # websockets default (proxy=True) would silently adopt whatever the
        # service environment holds, which is not what the person chose.
        async with connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {self._token}"},
            max_size=MAX_FRAME_BYTES,
            open_timeout=20,
            ping_interval=None,
            proxy=websocket_proxy(self._proxy),
        ) as socket:
            log.info("connected to the gateway", url=self.url)
            self._last_frame = time.monotonic()
            hello = await self._hello()
            hello.update(
                {"type": "hello", "protocol": PROTOCOL_VERSION, "client_version": __version__}
            )
            # Nothing has run since the snapshot was taken, so no change can fall between it and
            # this flag.
            self._greeting = True
            await socket.send(json.dumps(hello, ensure_ascii=False))
            early = await self._await_ack(socket)
            self._connected = True
            linkstate.record(linkstate.CONNECTED)
            # What reached the device ahead of the ack is served now, oldest first.
            for frame in early:
                await self._dispatch(frame)
            # Whatever happens on connect — a mirror scan, a registry read, a
            # backfill the gateway is waiting for — runs beside the pump, never
            # in front of it. Holding the pump back leaves every forwarded
            # request unanswered until it finishes, including the ones this work
            # is meant to answer.
            ready = self._begin_ready()
            try:
                await self._pump(socket)
            finally:
                if ready is not None and not ready.done():
                    ready.cancel()

    async def _await_ack(self, socket: ClientConnection) -> list[dict[str, Any]]:
        """Read until the gateway acknowledges the hello; what reached the device first is returned.

        A gateway can make a device reachable before it acknowledges the hello, so a request an
        app sent in that moment, or one it held while the link was down, may arrive ahead of the
        ack. It is a request like any other, and hanging up on it would lose it and cost a
        reconnect, so it is kept and served once the ack is in, in the order it came.
        """
        early: list[dict[str, Any]] = []
        held = 0
        async with asyncio.timeout(ACK_TIMEOUT):
            while True:
                raw = await socket.recv()
                frame = _decode(raw)
                self._last_frame = time.monotonic()
                if frame is None:
                    continue
                if frame.get("type") == "hello_ack":
                    return early
                if len(early) >= MAX_EARLY_FRAMES:
                    log.error("too many frames before hello_ack", frames=len(early))
                    raise RcError(
                        "too_large", f"more than {MAX_EARLY_FRAMES} frames before hello_ack"
                    )
                held += len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8"))
                if held > MAX_EARLY_BYTES:
                    log.error("too much data before hello_ack", bytes=held)
                    raise RcError(
                        "too_large", f"more than {MAX_EARLY_BYTES} bytes before hello_ack"
                    )
                early.append(frame)

    async def _ready(self) -> None:
        if self._on_ready is not None:
            await self._on_ready()

    def _begin_ready(self) -> asyncio.Task[None] | None:
        if self._on_ready is None:
            return None
        task: asyncio.Task[None] = asyncio.create_task(self._ready(), name="gateway-ready")
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        task.add_done_callback(_report_ready)
        return task

    async def _pump(self, socket: ClientConnection) -> None:
        """Race the three socket tasks so any one of them can end the session.

        The watchdog only helps if it can interrupt a receiver that is blocked
        on a half-open socket, which a plain task exception cannot do.
        """
        tasks = {
            asyncio.create_task(self._receiver(socket), name="receiver"),
            asyncio.create_task(self._sender(socket), name="sender"),
            asyncio.create_task(self._watchdog(), name="watchdog"),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error

    async def _sender(self, socket: ClientConnection) -> None:
        while True:
            payload = await self._queue.get()
            await socket.send(payload)

    async def _watchdog(self) -> None:
        """Fail the session when no frame at all has arrived for 60 s."""
        while True:
            await asyncio.sleep(WATCHDOG_TICK)
            if time.monotonic() - self._last_frame > SILENCE_TIMEOUT:
                raise RcError(
                    "timeout", f"no gateway frame for {SILENCE_TIMEOUT:.0f} s; reconnecting"
                )

    async def _receiver(self, socket: ClientConnection) -> None:
        async for raw in socket:
            self._last_frame = time.monotonic()
            frame = _decode(raw)
            if frame is not None:
                await self._dispatch(frame)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        frame_type = str(frame.get("type") or "")
        if frame_type == "ping":
            await self.send({"type": "pong"})
            return
        if frame_type in {"pong", "hello_ack"}:
            return
        signal = self._signals.get(frame_type)
        if signal is not None:
            task = asyncio.create_task(self._announce(signal, frame))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            return
        handler = self._handlers.get(frame_type)
        if handler is None:
            await self._reply(frame, RcError("unsupported", f"unknown request {frame_type}"))
            return
        task = asyncio.create_task(self._serve(handler, frame))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _announce(self, signal: Signal, frame: dict[str, Any]) -> None:
        """Apply a frame the gateway told us about; nobody is owed a reply."""
        try:
            await signal(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("gateway signal failed", signal=frame.get("type"))

    async def _serve(self, handler: Handler, frame: dict[str, Any]) -> None:
        try:
            result = await handler(frame)
        except RcError as exc:
            await self._reply(frame, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("request handler failed", request=frame.get("type"))
            await self._reply(frame, RcError("internal", type(exc).__name__))
            return
        await self._reply(frame, result)

    async def _reply(self, frame: dict[str, Any], outcome: dict[str, Any] | RcError) -> None:
        reply: dict[str, Any] = {"type": "reply", "id": frame.get("id")}
        if frame.get("from") is not None:
            reply["from"] = frame["from"]
        if isinstance(outcome, RcError):
            reply["ok"] = False
            reply["error"] = outcome.to_dict()
        else:
            reply["ok"] = True
            reply["result"] = outcome
        await self.send(reply)


def _decode(raw: str | bytes) -> dict[str, Any] | None:
    """One gateway frame, or None for what is not a JSON object."""
    try:
        frame = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.warning("dropping malformed gateway frame")
        return None
    return frame if isinstance(frame, dict) else None


def _report_ready(task: asyncio.Task[None]) -> None:
    """A connect-time task that failed must say so; nobody is awaiting it."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning("gateway connect work failed", error=describe_error(error))


ConnectionClosed = websockets.exceptions.ConnectionClosed
