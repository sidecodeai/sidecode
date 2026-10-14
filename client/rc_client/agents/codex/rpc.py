"""Newline-delimited JSON-RPC 2.0 over a `codex app-server` child process.

One reader task owns stdout. Server-to-client *requests* (approvals, questions)
are dispatched into detached tasks so a slow user decision never blocks the
stream, exactly as the upstream wrapper does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ...errors import RcError
from ...logging_setup import logger
from .provenance import DAEMON_CLIENT_NAME, EMBEDDED_CLIENT_NAME

log = logger("rc_client.codex.rpc")

REQUEST_TIMEOUT = 60.0
STARTUP_TIMEOUT = 30.0
MAX_LINE_BYTES = 32 * 1024 * 1024

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class CodexAppServer:
    """A resident app-server process speaking JSON-RPC on stdin/stdout."""

    def __init__(
        self,
        binary: str,
        *,
        on_notification: NotificationHandler | None = None,
        on_request: ServerRequestHandler | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._binary = binary
        self._on_notification = on_notification
        self._on_request = on_request
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> dict[str, Any]:
        env = dict(self._env) if self._env else None
        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=MAX_LINE_BYTES,
        )
        self._reader = asyncio.create_task(self._read_loop())
        info = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": EMBEDDED_CLIENT_NAME,
                    "title": DAEMON_CLIENT_NAME,
                    "version": "0.1.0",
                }
            },
            timeout=STARTUP_TIMEOUT,
        )
        await self.notify("initialized", {})
        return info

    async def close(self) -> None:
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        for task in list(self._tasks):
            task.cancel()
        if self._reader:
            self._reader.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            # Signalling a child that has just exited raises rather than
            # returning, and this runs while a session is being torn down.
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self._process = None

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RcError("agent_unavailable", "codex app-server is not running")
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RcError("timeout", f"codex {method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("dropping malformed app-server line", bytes=len(line))
                    continue
                self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("codex app-server reader failed")
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RcError("agent_unavailable", "codex app-server exited"))
            self._pending.clear()

    def _handle(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        method = message.get("method")
        if message_id is not None and method is None:
            future = self._pending.pop(int(message_id), None)
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"] or {}
                future.set_exception(
                    RcError("agent_unavailable", str(error.get("message") or "codex error"))
                )
            else:
                future.set_result(message.get("result") or {})
            return
        if method is None:
            return
        params = message.get("params") or {}
        if message_id is None:
            if self._on_notification:
                self._spawn(self._on_notification(method, params))
            return
        self._spawn(self._serve_request(message_id, method, params))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(_swallow(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve_request(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        if self._on_request is None:
            await self.respond_error(request_id, -32601, f"unhandled request {method}")
            return
        try:
            result = await self._on_request(method, params)
        except Exception as exc:
            log.warning("codex server request failed", method=method)
            if not self._closed:
                await self.respond_error(request_id, -32000, str(exc)[:200])
            return
        if not self._closed:
            await self.respond(request_id, result)


async def _swallow(coro: Awaitable[Any]) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("codex background task failed")


async def one_shot(
    binary: str, method: str, params: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    """Run one request against a throwaway app-server (no thread, no tokens)."""
    server = CodexAppServer(binary)
    try:
        await server.start()
        return await server.request(method, params, timeout=timeout)
    finally:
        await server.close()
