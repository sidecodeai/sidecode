"""The gateway's WebSocket to the Live API: one connection per call, and the call's whole session.

The session lives exactly as long as this socket (OpenAI's "primary WebSocket"), so closing it ends
the session; there is no separately created session that could be left behind. Nothing that crosses
it is logged: only event types and close codes ever are.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from ..logging import logger
from .config import LiveConfig
from .errors import LiveError, safe_code

log = logger("rc_gateway.live.upstream")

#: What the websockets library logs for this socket. At debug it would log every frame, and the
#: handshake with the key in it, so it is pinned above that whatever the gateway's level is.
_WIRE_LOGGER = logging.getLogger("rc_gateway.live.wire")
_WIRE_LOGGER.setLevel(logging.WARNING)

OPEN_TIMEOUT_SECONDS = 10.0
STARTED_TIMEOUT_SECONDS = 10.0
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 3.0
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class UpstreamLink(Protocol):
    async def send(self, event: dict[str, Any]) -> None:
        """Send one event. Raises ``LiveError`` when the socket is gone or stuck."""
        ...

    async def receive(self) -> dict[str, Any]:
        """The next event. Raises ``LiveError("upstream_lost")`` when the socket is gone."""
        ...

    async def close(self) -> None: ...


Session = dict[str, Any]
UpstreamConnector = Callable[[LiveConfig, Session], Awaitable[tuple[UpstreamLink, dict[str, Any]]]]


class OpenAiUpstream:
    def __init__(self, socket: ClientConnection) -> None:
        self._socket = socket

    async def send(self, event: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(self._socket.send(json.dumps(event)), SEND_TIMEOUT_SECONDS)
        except ConnectionClosed as exc:
            raise LiveError("upstream_lost", "the voice service closed the connection") from exc
        except TimeoutError as exc:
            raise LiveError("upstream_stalled", "the voice service stopped reading") from exc

    async def receive(self) -> dict[str, Any]:
        while True:
            try:
                raw = await self._socket.recv()
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                log.info("live upstream closed", close_code=code)
                raise LiveError("upstream_lost", "the voice service closed the connection") from exc
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                return event

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._socket.close(), CLOSE_TIMEOUT_SECONDS)


async def connect_upstream(
    config: LiveConfig, session: Session
) -> tuple[UpstreamLink, dict[str, Any]]:
    """Open the socket, start the session and wait until the service says it has started."""
    try:
        socket = await connect(
            config.socket_url,
            additional_headers={"Authorization": f"Bearer {config.api_key}"},
            open_timeout=OPEN_TIMEOUT_SECONDS,
            max_size=MAX_MESSAGE_BYTES,
            compression=None,
            logger=_WIRE_LOGGER,
        )
    except InvalidStatus as exc:
        log.warning("live upstream refused the connection", status=exc.response.status_code)
        raise LiveError("upstream_refused", "the voice service refused the call") from exc
    except (OSError, WebSocketException, TimeoutError) as exc:
        log.warning("live upstream unreachable", error=type(exc).__name__)
        raise LiveError("upstream_unavailable", "the voice service is unreachable") from exc
    upstream = OpenAiUpstream(socket)
    try:
        await upstream.send({"type": "session.start", "event_id": "start", "session": session})
        started = await asyncio.wait_for(_await_started(upstream), STARTED_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        await upstream.close()
        raise LiveError("upstream_unavailable", "the voice service did not start") from exc
    except BaseException:
        # A failure, or this task being cancelled, with the socket open: it is closed, not leaked.
        await upstream.close()
        raise
    return upstream, started


async def _await_started(upstream: UpstreamLink) -> dict[str, Any]:
    while True:
        event = await upstream.receive()
        kind = event.get("type")
        if kind == "session.started":
            return event
        if kind in ("error", "session.closed"):
            error = event.get("error")
            code = safe_code(error.get("code") if isinstance(error, dict) else None)
            log.warning("live session did not start", event=kind, error_code=code)
            raise LiveError("upstream_refused", "the voice service refused the call")
