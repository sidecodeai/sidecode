"""An in-process stand-in for the shared Codex app-server daemon.

It speaks the same thing the real one does — JSON-RPC 2.0 as WebSocket text
frames over an AF_UNIX socket — so the client under test exercises its real
handshake, framing and correlation rather than a mock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, unix_serve

from rc_client.agents.codex.daemon.terminals import TerminalScan

Responder = Callable[[str, dict[str, Any]], Any]


@dataclass
class FakeTerminals:
    """The TUI process scan, which is the only signal a TUI exit ever gives.

    The daemon itself says nothing when a terminal leaves, so a test that wants
    to stage one empties `cwds` and lets the next scan run. One TUI per
    directory unless `counts` says otherwise.
    """

    cwds: set[str] = field(default_factory=set)
    complete: bool = True
    scans: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    async def __call__(self) -> TerminalScan:
        self.scans += 1
        found = {os.path.realpath(cwd): self.counts.get(cwd, 1) for cwd in self.cwds}
        return TerminalScan(cwds=found, complete=self.complete)


class FakeDaemon:
    """One socket, one connection at a time, every message recorded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies: dict[str, Any] = {}
        # Methods the daemon refuses, by message. `thread/resume` is the one
        # that matters: it refuses a thread whose first turn has not run.
        self.errors: dict[str, str] = {}
        self.responder: Responder | None = None
        # Requests sent right behind the next answer to `thread/resume`, the way
        # Codex replays what is pending on a thread to the connection that has
        # just resumed it.
        self.replays: list[tuple[Any, str, dict[str, Any]]] = []
        self.answers: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.connections = 0
        # What the real daemon stamps on the echo of a prompt. `None` is what it
        # does for a prompt submitted over the control socket; a string is what a
        # TUI's own message carries.
        self.echo_client_id: str | None = None
        self.echoes = 0
        self._server: Any = None
        self._live: ServerConnection | None = None

    async def start(self) -> None:
        self._server = await unix_serve(self._serve, path=str(self.path), compression=None)

    async def stop(self) -> None:
        await self.drop()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def drop(self) -> None:
        """Close the live connection, which is what a daemon restart looks like."""
        connection, self._live = self._live, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    async def _serve(self, connection: ServerConnection) -> None:
        self.connections += 1
        self._live = connection
        try:
            async for raw in connection:
                message = json.loads(raw)
                await self._handle(connection, message)
        except Exception:
            return

    async def _handle(self, connection: ServerConnection, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        if method:
            self.calls.append((method, params))
        if "id" not in message:
            return
        if method in self.errors:
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32000, "message": self.errors[method]},
                    }
                )
            )
            return
        if method:
            result = self._result(method, params)
            await connection.send(
                json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
            )
            if method == "thread/resume":
                await self._replay()
            return
        await self.answers.put(message)

    async def _replay(self) -> None:
        replays, self.replays = self.replays, []
        for request_id, method, params in replays:
            await self.ask(request_id, method, params)

    def _result(self, method: str, params: dict[str, Any]) -> Any:
        if self.responder is not None:
            produced = self.responder(method, params)
            if produced is not None:
                return produced
        if method == "initialize":
            return {"userAgent": "fake", "codexHome": "/tmp/codex"}
        if method == "turn/start":
            self._thread_status(str(params.get("threadId") or ""), {"type": "active"})
        return self.replies.get(method, {})

    def _thread_status(self, thread_id: str, status: dict[str, Any]) -> None:
        """Keep index snapshots consistent with the live turn the fake serves."""
        for reply in self.replies.values():
            if not isinstance(reply, dict):
                continue
            rows = [reply.get("thread"), *(reply.get("data") or [])]
            for row in rows:
                if isinstance(row, dict) and row.get("id") == thread_id:
                    row["status"] = status

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        connection = self._live
        assert connection is not None
        if method in {"turn/started", "turn/completed", "thread/status/changed"}:
            status = params.get("status")
            if not isinstance(status, dict):
                status = {"type": "active" if method == "turn/started" else "idle"}
            self._thread_status(str(params.get("threadId") or ""), status)
        await connection.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    async def echo_prompt(
        self,
        thread_id: str,
        text: str,
        *,
        item_id: str | None = None,
        client_id: str | None = None,
    ) -> None:
        """Replay a prompt the way the daemon does: the same item, twice."""
        self.echoes += 1
        item = {
            "type": "userMessage",
            "id": item_id or f"item-{self.echoes}",
            "clientId": self.echo_client_id if client_id is None else client_id,
            "content": [{"type": "text", "text": text, "text_elements": []}],
        }
        for method, stamp in (("item/started", "startedAtMs"), ("item/completed", "completedAtMs")):
            await self.notify(
                method, {"threadId": thread_id, "item": dict(item), "turnId": "turn-1", stamp: 1}
            )

    async def ask(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Send a server-to-client request, the way an approval arrives."""
        connection = self._live
        assert connection is not None
        await connection.send(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        )

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    async def wait_for_call(self, method: str, timeout: float = 2.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            found = self.sent(method)
            if found:
                return found[-1]
            await asyncio.sleep(0.01)
        raise AssertionError(f"the client never called {method}")
