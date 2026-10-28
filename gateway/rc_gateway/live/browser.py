"""The browser's end of a call: one WebSocket, written and read one frame at a time."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import WebSocket

from .errors import LiveError
from .protocol import MAX_CONTROL_BYTES, close_code_for, ended_message

#: A browser that does not take a frame in this long is not listening.
SEND_TIMEOUT_SECONDS = 10.0
#: The last words to a browser that is going away, and the close after them, must not hold the
#: call's own shutdown up: a browser that stopped reading would otherwise hold them for ever.
FAREWELL_TIMEOUT_SECONDS = 2.0
CLOSE_TIMEOUT_SECONDS = 2.0


class BrowserLink(Protocol):
    async def send_bytes(self, data: bytes) -> None:
        """Send assistant audio. Raises ``LiveError`` once the browser is gone or stuck."""
        ...

    async def send_json(self, message: dict[str, Any]) -> None: ...

    async def receive(self) -> bytes | dict[str, Any] | None:
        """A microphone frame, a command, or ``None`` once the browser has closed."""
        ...

    async def end(self, reason: str) -> None:
        """Tell the browser why, then close the socket with the code that fits the reason."""
        ...


class WebSocketLink:
    def __init__(
        self,
        ws: WebSocket,
        *,
        send_timeout: float = SEND_TIMEOUT_SECONDS,
        farewell_timeout: float = FAREWELL_TIMEOUT_SECONDS,
        close_timeout: float = CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self._ws = ws
        self._send_timeout = send_timeout
        self._farewell_timeout = farewell_timeout
        self._close_timeout = close_timeout
        self._lock = asyncio.Lock()
        self._gone = False

    async def send_bytes(self, data: bytes) -> None:
        await self._send(lambda: self._ws.send_bytes(data), self._send_timeout)

    async def send_json(self, message: dict[str, Any]) -> None:
        await self._send_text(message, self._send_timeout)

    async def _send_text(self, message: dict[str, Any], timeout: float) -> None:
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        await self._send(lambda: self._ws.send_text(text), timeout)

    async def _send(self, sending: Callable[[], Awaitable[None]], timeout: float) -> None:
        # One frame at a time: the ASGI server does not promise that two tasks can send at once.
        async with self._lock:
            if self._gone:
                raise LiveError("browser_closed", "the browser has gone")
            try:
                await asyncio.wait_for(sending(), timeout)
            except TimeoutError as exc:
                self._gone = True
                raise LiveError("browser_stalled", "the browser stopped reading") from exc
            except Exception as exc:
                self._gone = True
                raise LiveError("browser_closed", "the browser has gone") from exc

    async def receive(self) -> bytes | dict[str, Any] | None:
        if self._gone:
            return None
        try:
            message = await self._ws.receive()
        except Exception:
            self._gone = True
            return None
        if message.get("type") == "websocket.disconnect":
            self._gone = True
            return None
        data = message.get("bytes")
        if data is not None:
            return bytes(data)
        return _command(message.get("text"))

    async def end(self, reason: str) -> None:
        with contextlib.suppress(LiveError):
            await self._send_text(ended_message(reason), self._farewell_timeout)
        self._gone = True
        # Whatever the server says about a socket the browser has already dropped is not news, and
        # a close the server cannot finish (the browser stopped reading) is given up on.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self._ws.close(code=close_code_for(reason), reason=reason[:100]),
                self._close_timeout,
            )


def _command(text: str | None) -> dict[str, Any]:
    if text is None or len(text) > MAX_CONTROL_BYTES:
        raise LiveError("protocol_error", "a control message is empty or too large")
    try:
        command = json.loads(text)
    except ValueError as exc:
        raise LiveError("protocol_error", "a control message is not JSON") from exc
    if not isinstance(command, dict):
        raise LiveError("protocol_error", "a control message is not an object")
    return command
