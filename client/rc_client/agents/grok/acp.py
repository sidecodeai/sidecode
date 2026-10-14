"""Newline-delimited JSON-RPC 2.0 against `agent agent stdio`.

Grok speaks the Agent Client Protocol on stdin and stdout. One reader task owns
stdout; agent-to-client *requests* (permission prompts) are dispatched into
detached tasks, because answering one waits for a person and must never block
the stream that is still delivering the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ...errors import RcError
from ...logging_setup import logger

log = logger("rc_client.grok.acp")

REQUEST_TIMEOUT = 60.0
STARTUP_TIMEOUT = 30.0
# A prompt runs until the turn ends, so it gets no deadline of its own.
MAX_LINE_BYTES = 32 * 1024 * 1024

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

PROTOCOL_VERSION = 1


class GrokTransport(Protocol):
    """What a session needs of its connection, whoever owns the process.

    A session this device started for itself owns a private `GrokAgent`; one
    that joined the machine's leader shares that connection with every other
    client of it (A28). Both answer requests and carry notifications, and the
    runner never needs to know which it has.
    """

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = ...
    ) -> dict[str, Any]: ...

    async def notify(self, method: str, params: dict[str, Any]) -> None: ...


CLIENT_CAPABILITIES = {
    # The device never serves files or terminals back to the agent: Grok runs
    # on the same machine and reads them itself.
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


class GrokAgent:
    """One `agent agent stdio` child process."""

    def __init__(
        self,
        binary: str,
        *,
        cwd: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        leader: bool = False,
        on_notification: NotificationHandler | None = None,
        on_request: ServerRequestHandler | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = cwd
        self._args = list(args or [])
        self._leader = leader
        self._env = env
        self._on_notification = on_notification
        self._on_request = on_request
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
        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "agent",
            # A private child never joins a leader another client started: a
            # shared backend would put this session's tools in someone else's
            # process. `--leader` is the opposite, and deliberate: it is how the
            # device joins the sessions a terminal is running (A28).
            "--leader" if self._leader else "--no-leader",
            *self._args,
            "stdio",
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            limit=MAX_LINE_BYTES,
        )
        self._reader = asyncio.create_task(self._read_loop())
        return await self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": CLIENT_CAPABILITIES},
            timeout=STARTUP_TIMEOUT,
        )

    async def close(self) -> None:
        self._closed = True
        # Let every task already created take its first step. Cancelling one
        # that has not started drops the notification coroutine inside it
        # unawaited, which Python reports as a warning at collection time.
        await asyncio.sleep(0)
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

    # ---------------------------------------------------------------- writes

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RcError("agent_unavailable", "the Grok agent is not running")
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RcError("timeout", f"grok {method} timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    # ----------------------------------------------------------------- reads

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
                    log.warning("dropping malformed grok line", bytes=len(line))
                    continue
                if isinstance(message, dict):
                    self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("grok reader failed")
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RcError("agent_unavailable", "the Grok agent exited"))
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
                    RcError("agent_unavailable", str(error.get("message") or "grok error"))
                )
            else:
                result = message.get("result")
                future.set_result(result if isinstance(result, dict) else {})
            return
        if method is None:
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if message_id is None:
            if self._on_notification:
                self._spawn(self._on_notification(str(method), params))
            return
        self._spawn(self._serve_request(message_id, str(method), params))

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
            log.warning("grok server request failed", method=method)
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
        log.exception("grok background task failed")
