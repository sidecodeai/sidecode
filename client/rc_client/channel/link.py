"""The bridge's connection to the daemon: buffered, reconnecting, line framed.

The CLI outlives daemon restarts, so a bridge that cannot reach the socket keeps
serving Claude Code and retries in the background. Frames produced meanwhile are
buffered, and the registration frame is always replayed first on a new
connection so the daemon can rebuild its view.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import wire

FIRST_BACKOFF = 0.25
MAX_BACKOFF = 5.0
MAX_BUFFERED = 64

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class DaemonLink:
    """A reconnecting client for the daemon's channel socket."""

    def __init__(self, path: Path, on_message: Handler) -> None:
        self._path = path
        self._on_message = on_message
        self._registration: dict[str, Any] | None = None
        self._buffer: list[dict[str, Any]] = []
        self._pending = asyncio.Event()
        self._registered = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def register(self, message: dict[str, Any]) -> None:
        """Record the frame that identifies this bridge; sent first on every connect."""
        self._registration = message
        self._registered.set()

    def send(self, message: dict[str, Any]) -> None:
        """Queue one frame: written now when connected, on reconnect otherwise."""
        self._buffer.append(message)
        del self._buffer[:-MAX_BUFFERED]
        self._pending.set()

    # ----------------------------------------------------------------- inner

    async def _run(self) -> None:
        backoff = FIRST_BACKOFF
        while True:
            await self._registered.wait()
            try:
                await self._session()
                backoff = FIRST_BACKOFF
            except asyncio.CancelledError:
                raise
            except OSError:
                pass
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)

    async def _session(self) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self._path))
        sender = asyncio.create_task(self._send_loop(writer))
        try:
            async for line in reader:
                message = wire.decode(line)
                if message is not None:
                    await self._on_message(message)
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send_loop(self, writer: asyncio.StreamWriter) -> None:
        if self._registration is not None:
            writer.write(wire.encode(self._registration))
        while True:
            self._pending.clear()
            for message in self._buffer:
                writer.write(wire.encode(message))
            self._buffer.clear()
            await writer.drain()
            await self._pending.wait()
