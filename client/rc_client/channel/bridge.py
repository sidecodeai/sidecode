"""`rc-client channel`: the stdio MCP server Claude Code spawns for the channel.

It never writes to stdout except JSON-RPC, never logs prompt text, and exits the
moment stdin closes. Claude Code does not reap this process, so a bridge that
outlives its parent would hold the daemon socket open forever.

The bridge registers with the daemon as soon as Claude Code says `initialize`,
but Claude Code only listens for channel messages once it has listed this
server's tools, and drops one that arrives before that without a word. A
message the daemon injects that early is held here and written, in order, once
Claude Code listens. Each injection is confirmed to the daemon once it is
written, which is what a turn's `Stop` hook waits on (amendment A51).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from typing import Any

from . import paths, rpc, wire
from .link import DaemonLink

CLOSE_GRACE = 0.05
SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
PROJECT_ENV = "CLAUDE_PROJECT_DIR"
# How long after answering `tools/list` Claude Code is given to start listening,
# and how long a bridge waits for that request before writing what it holds.
LISTEN_GRACE = 0.25
LISTEN_TIMEOUT = 3.0


def warn(text: str) -> None:
    """The only diagnostics this process produces, and never about content."""
    print(f"rc-client channel: {text}", file=sys.stderr, flush=True)


class ChannelBridge:
    """Wire Claude Code's stdio JSON-RPC to the daemon's channel socket."""

    def __init__(self, link: DaemonLink, session_id: str, cwd: str) -> None:
        self._link = link
        self._session_id = session_id
        self._cwd = cwd
        self._registered = False
        # Injected messages Claude Code is not listening for yet, oldest first,
        # with the id each is confirmed by; `None` once it listens and
        # everything goes straight out.
        self._held: list[tuple[str, dict[str, Any]]] | None = []
        self._listed = asyncio.Event()

    # ------------------------------------------------------- Claude -> daemon

    def on_rpc(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one inbound JSON-RPC frame, returning the frame to write back."""
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == rpc.PERMISSION_REQUEST_NOTIFICATION:
            self._link.send(wire.permission_request(params))
            return None
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            self._start_registration(rpc.client_version(params))
        elif method == "tools/list":
            self._listed.set()
        return rpc.handle_request(method, message.get("id"), params)

    async def listen(self) -> None:
        """Write what is held once Claude Code listens for channel messages.

        It registers its handler right after the tool list it asked for comes
        back, so the grace runs from the answer; a CLI that never asks is
        written to after the timeout, as every message was before.
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._listed.wait(), LISTEN_TIMEOUT)
        await asyncio.sleep(LISTEN_GRACE)
        held, self._held = self._held or [], None
        for message_id, frame in held:
            write(frame)
            self._written(message_id)

    def _start_registration(self, claude_version: str | None) -> None:
        if self._registered or not self._session_id:
            return
        self._registered = True
        self._link.register(
            wire.register(self._session_id, self._cwd, os.getppid(), claude_version)
        )
        self._link.start()

    # ------------------------------------------------------- daemon -> Claude

    async def on_daemon(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == wire.INJECT:
            message_id = str(message.get("message_id") or "")
            frame = rpc.channel_message(message_id, str(message.get("text") or ""))
            if self._held is None:
                write(frame)
                self._written(message_id)
            else:
                self._held.append((message_id, frame))
        elif kind == wire.PERMISSION:
            write(
                rpc.permission_verdict(
                    str(message.get("request_id") or ""), str(message.get("behavior") or "deny")
                )
            )

    def _written(self, message_id: str) -> None:
        """Claude Code has the injection on its stdin now; the daemon may be waiting on it."""
        if message_id:
            self._link.send(wire.injected(message_id))


def write(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def _stdin_lines() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=wire.MAX_LINE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


def _install_exit_signals() -> None:
    loop = asyncio.get_running_loop()
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(number, lambda: os._exit(0))


async def serve() -> int:
    """Run until stdin closes; the process then exits without waiting on anything."""
    session_id = os.environ.get(SESSION_ENV, "")
    cwd = os.environ.get(PROJECT_ENV) or os.getcwd()
    if not session_id:
        warn(f"{SESSION_ENV} is not set; this session cannot be controlled remotely")

    socket = paths.socket_path()
    bridge: ChannelBridge | None = None

    async def on_daemon(message: dict[str, Any]) -> None:
        if bridge is not None:
            await bridge.on_daemon(message)

    link = DaemonLink(socket, on_daemon)
    bridge = ChannelBridge(link, session_id, cwd)
    _install_exit_signals()
    listening = asyncio.create_task(bridge.listen())

    reader = await _stdin_lines()
    async for line in reader:
        message = wire.decode(line)
        if message is None:
            continue
        try:
            outbound = bridge.on_rpc(message)
        except Exception as exc:  # pragma: no cover - defensive
            warn(f"failed to handle {message.get('method')!r}: {type(exc).__name__}")
            continue
        if outbound is not None:
            write(outbound)

    listening.cancel()
    # The daemon also treats socket EOF as a close; this frame just makes the
    # reason explicit when it arrives in time.
    link.send(wire.closed())
    await asyncio.sleep(CLOSE_GRACE)
    with contextlib.suppress(Exception):
        await link.stop()
    return 0


def main() -> int:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve())
    # Claude Code never reaps this process, and a lingering bridge holds a stale
    # registration on the daemon; leave immediately rather than unwinding.
    sys.stdout.flush()
    os._exit(0)
