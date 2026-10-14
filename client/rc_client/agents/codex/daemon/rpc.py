"""JSON-RPC 2.0 over the shared daemon's WebSocket control socket.

One connection carries every thread, so the reader loop must never block:
server-to-client requests (approvals, questions) wait on a human and are
therefore dispatched into their own tasks, exactly as the per-session
app-server client does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from ....errors import RcError
from ....logging_setup import logger
from ..provenance import DAEMON_CLIENT_NAME
from .transport import open_connection

log = logger("rc_client.codex.daemon")

REQUEST_TIMEOUT = 60.0
HANDSHAKE_TIMEOUT = 15.0
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[Any, str, dict[str, Any]], Awaitable[dict[str, Any]]]
ConnectedHandler = Callable[[], Awaitable[None]]


def initialize_params(version: str) -> dict[str, Any]:
    """Initialize the connection with the device's stable client identity."""
    return {
        "clientInfo": {
            "name": DAEMON_CLIENT_NAME,
            "title": DAEMON_CLIENT_NAME,
            "version": version,
        },
        "capabilities": {"experimentalApi": True},
    }


class DaemonClient:
    """A reconnecting client for one shared Codex app-server daemon."""

    def __init__(
        self,
        version: str,
        *,
        on_notification: NotificationHandler,
        on_request: ServerRequestHandler,
        on_connected: ConnectedHandler | None = None,
        socket: Path | None = None,
    ) -> None:
        self._version = version
        self._on_notification = on_notification
        self._on_request = on_request
        self._on_connected = on_connected
        self._socket = socket
        self._connection: ClientConnection | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()
        self._ready = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._ready.is_set()

    async def start(self) -> dict[str, Any]:
        """Connect and handshake once, then keep the connection up in the background.

        The first attempt is the caller's mode decision, so its failure is
        raised rather than retried: no daemon means the fallback path.
        """
        info = await self._connect()
        self._supervisor = asyncio.create_task(self._supervise())
        return info

    async def close(self) -> None:
        self._stopping.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._teardown()

    async def _connect(self) -> dict[str, Any]:
        connection = await open_connection(self._socket)
        self._connection = connection
        self._ready.set()
        self._reader = asyncio.create_task(self._read_loop(connection))
        try:
            info = await self.request(
                "initialize", initialize_params(self._version), timeout=HANDSHAKE_TIMEOUT
            )
            await self.notify("initialized", {})
        except Exception:
            await self._teardown()
            raise
        log.info("attached to the shared codex daemon", codex_home=str(info.get("codexHome") or ""))
        return info

    async def _teardown(self) -> None:
        self._ready.clear()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._fail_pending("the codex daemon connection closed")
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

    async def _supervise(self) -> None:
        """Reconnect with backoff for as long as the client is wanted."""
        delay = RECONNECT_MIN
        while not self._stopping.is_set():
            reader = self._reader
            if reader is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
            if self._stopping.is_set():
                return
            await self._teardown()
            await asyncio.sleep(delay)
            try:
                await self._connect()
            except (OSError, ConnectionClosed, RcError, TimeoutError) as exc:
                delay = min(delay * 2, RECONNECT_MAX)
                log.warning("codex daemon reconnect failed", error=str(exc)[:200])
                continue
            delay = RECONNECT_MIN
            if self._on_connected is not None:
                self._spawn(self._on_connected())

    # ---------------------------------------------------------------- frames

    async def _send(self, payload: dict[str, Any]) -> None:
        connection = self._connection
        if connection is None:
            raise RcError("agent_unavailable", "the codex daemon is not connected")
        try:
            await connection.send(json.dumps(payload, ensure_ascii=False))
        except (ConnectionClosed, OSError) as exc:
            raise RcError("agent_unavailable", "the codex daemon connection dropped") from exc

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RcError("timeout", f"codex {method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        with contextlib.suppress(RcError):
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        with contextlib.suppress(RcError):
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )

    # ------------------------------------------------------------ read loop

    async def _read_loop(self, connection: ClientConnection) -> None:
        try:
            async for raw in connection:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("dropping a malformed daemon frame", bytes=len(raw))
                    continue
                if isinstance(message, dict):
                    self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("codex daemon reader stopped", error=str(exc)[:200])
        finally:
            self._ready.clear()
            self._fail_pending("the codex daemon connection closed")

    def _fail_pending(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RcError("agent_unavailable", message))
        self._pending.clear()

    def _handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")
        if method is None:
            self._resolve(message_id, message)
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if message_id is None:
            self._spawn(self._on_notification(str(method), params))
            return
        self._spawn(self._serve(message_id, str(method), params))

    def _resolve(self, message_id: Any, message: dict[str, Any]) -> None:
        try:
            key = int(message_id)
        except (TypeError, ValueError):
            return
        future = self._pending.pop(key, None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error") or {}
            future.set_exception(
                RcError("agent_unavailable", str(error.get("message") or "codex daemon error"))
            )
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    async def _serve(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self._on_request(request_id, method, params)
        except Exception as exc:
            log.warning("codex daemon request handler failed", method=method)
            await self.respond_error(request_id, -32000, str(exc)[:200])
            return
        await self.respond(request_id, result)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(_swallow(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def _swallow(coro: Awaitable[Any]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("codex daemon background task failed")


async def handshake_ok(version: str, socket: Path | None = None) -> bool:
    """Whether the daemon actually answers, which is what `attach_ready` means.

    A socket file left behind by a dead daemon looks exactly like a live one, so
    nothing short of a completed handshake counts.
    """
    try:
        connection = await open_connection(socket)
    except Exception:
        return False
    try:
        probe = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        await connection.send(json.dumps({**probe, "params": initialize_params(version)}))
        while True:
            raw = await asyncio.wait_for(connection.recv(), timeout=HANDSHAKE_TIMEOUT)
            message = json.loads(raw)
            if isinstance(message, dict) and message.get("id") == 1:
                return "result" in message
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await connection.close()
