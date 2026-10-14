"""Reading text frames from a socket that may send binary ones.

Starlette's ``receive_text`` ends with ``message["text"]``, so a binary frame raises ``KeyError``
inside the handler: an authenticated peer could drop its own link with a traceback at ERROR level
and no close code, and on ``/ws/device`` that costs the device its link plus the A13 grace period.
The protocol is text-only, so a binary frame is ignored like a malformed one — and a peer that
keeps sending them is told so with 1008 rather than left guessing.
"""

from __future__ import annotations

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


class BinaryFrames(RuntimeError):
    """The peer sent nothing but binary frames for long enough to call it a protocol error."""


#: Consecutive binary frames past which the socket is closed with ``CLOSE_PROTOCOL_ERROR``.
MAX_CONSECUTIVE_BINARY = 3


class TextReader:
    """One socket's text stream, counting the binary frames it skips."""

    def __init__(self, ws: WebSocket, *, limit: int = MAX_CONSECUTIVE_BINARY) -> None:
        self.ws = ws
        self.limit = limit
        self.skipped = 0

    async def read(self) -> str:
        """The next text frame, skipping binary ones. Raises on disconnect or too many skips."""
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(int(message.get("code", 1000)))
            text = message.get("text")
            if isinstance(text, str):
                self.skipped = 0
                return text
            self.skipped += 1
            if self.skipped >= self.limit:
                raise BinaryFrames(f"{self.skipped} binary frames in a row")
