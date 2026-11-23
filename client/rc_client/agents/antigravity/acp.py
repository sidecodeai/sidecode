"""Newline-delimited JSON-RPC 2.0 with `agy acp`, the Agent Client Protocol server of Antigravity.

The device is an ACP client as an editor is: it starts `agy acp` with no other argument, names
itself `sidecode` in `initialize`, declares form questions and neither file system nor terminal
access, so `agy` reads, writes and runs everything itself (A64). It never calls `authenticate`.

Everything `agy` writes — its answers, its notifications and the requests it makes of the
client — is handled in the order it arrived, by one task: a turn's last words are published before
the answer that ends the turn, and a replay before the `session/load` answer that follows it. So a
handler never waits on an answer from `agy` itself, and a request that waits for a person (an
approval, a form) puts its card up and hands the wait to a task of its own. `agy` numbers its own
requests with strings, which a reply echoes as they came.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ... import __version__
from ...errors import RcError
from ...logging_setup import logger

log = logger("rc_client.antigravity.acp")

PROTOCOL_VERSION = 1
REQUEST_TIMEOUT = 60.0
STARTUP_TIMEOUT = 30.0
# How long a stopping `agy` is given once its input is closed, and again after SIGTERM: it saves
# the conversation when its client goes, and has always been gone within a tenth of a second.
EXIT_GRACE = 5.0
MAX_LINE_BYTES = 32 * 1024 * 1024
NOT_RUNNING = "agy is not running"

CLIENT_INFO = {"name": "sidecode", "title": "SideCode", "version": __version__}
CLIENT_CAPABILITIES: dict[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
    "elicitation": {"form": {}},
}

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
# Given the request's own id, its method and its params; it answers through `respond`.
RequestHandler = Callable[[Any, str, dict[str, Any]], Awaitable[None]]
# Told which connection's `agy` went away by itself.
ExitHandler = Callable[["AgyConnection"], Awaitable[None]]


class AgyError(RcError):
    """An error `agy` answered a request with, and the JSON-RPC code it gave."""

    def __init__(self, message: str, rpc_code: Any) -> None:
        super().__init__("agent_unavailable", message)
        exact = isinstance(rpc_code, int) and not isinstance(rpc_code, bool)
        self.rpc_code: int | None = rpc_code if exact else None


class AgyExited(RcError):
    """`agy` went away before it answered."""

    def __init__(self) -> None:
        super().__init__("agent_unavailable", "agy exited")


def initialize_params() -> dict[str, Any]:
    """What the device says of itself, the same in every process it starts."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "clientInfo": dict(CLIENT_INFO),
        "clientCapabilities": copy.deepcopy(CLIENT_CAPABILITIES),
    }


@dataclass(slots=True)
class _Inbound:
    """One line `agy` wrote, or `eof` once it wrote its last."""

    message: dict[str, Any] = field(default_factory=dict)
    eof: bool = False


class AgyConnection:
    """One `agy acp` child process, and the task that handles what it writes, in order."""

    def __init__(
        self,
        binary: str,
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
        on_exit: ExitHandler | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = cwd
        self._env = env
        self._on_notification = on_notification
        self._on_request = on_request
        self._on_exit = on_exit
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._inbox: asyncio.Queue[_Inbound] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._writing = asyncio.Lock()
        self._closing = False
        self.agent_version: str | None = None

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None and not self._closing

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def start(self) -> dict[str, Any]:
        """Start `agy acp` in the folder given, and say who the device is."""
        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "acp",
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            limit=MAX_LINE_BYTES,
        )
        self._tasks = [
            asyncio.create_task(self._read(), name="agy-reader"),
            asyncio.create_task(self._dispatch(), name="agy-dispatch"),
        ]
        result = await self.request("initialize", initialize_params(), timeout=STARTUP_TIMEOUT)
        info = result.get("agentInfo")
        version = info.get("version") if isinstance(info, dict) else None
        self.agent_version = version if isinstance(version, str) and version else None
        return result

    async def close(self) -> None:
        """Let `agy` go: its input closes, and it saves and exits; a signal only if it does not."""
        self._closing = True
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            await _ended(process)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self._fail_pending()

    # ---------------------------------------------------------------- writes

    async def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RcError("agent_unavailable", NOT_RUNNING)
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._writing:
            try:
                process.stdin.write(line)
                await process.stdin.drain()
            except (ConnectionError, RuntimeError) as exc:
                raise RcError("agent_unavailable", NOT_RUNNING) from exc

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        """Ask `agy` something and wait for its answer; `timeout` None waits as long as it takes."""
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RcError("timeout", f"agy did not answer {method} in time") from exc
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        """Answer one of `agy`'s requests; one it can no longer hear is let go."""
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except RcError:
            log.info("an answer to agy was dropped: it is not running")

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
            )
        except RcError:
            log.info("an error answer to agy was dropped: it is not running")

    # ----------------------------------------------------------------- reads

    async def _read(self) -> None:
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
                    log.warning("dropping a malformed line from agy", bytes=len(line))
                    continue
                if isinstance(message, dict):
                    self._inbox.put_nowait(_Inbound(message))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reading agy failed")
        self._inbox.put_nowait(_Inbound(eof=True))

    async def _dispatch(self) -> None:
        while True:
            item = await self._inbox.get()
            if item.eof:
                break
            try:
                await self._handle(item.message)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("handling a message from agy failed")
        self._fail_pending()
        if not self._closing and self._on_exit is not None:
            await self._on_exit(self)

    async def _handle(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        method = message.get("method")
        if method is None:
            if message_id is not None:
                self._answered(message_id, message)
            return
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if message_id is None:
            if self._on_notification is not None:
                await self._on_notification(str(method), params)
            return
        if self._on_request is None:
            await self.respond_error(message_id, -32601, f"unhandled request {method}")
            return
        try:
            await self._on_request(message_id, str(method), params)
        except Exception as exc:
            log.warning("an agy request could not be served", method=method)
            await self.respond_error(message_id, -32603, str(exc)[:200])

    def _answered(self, message_id: Any, message: dict[str, Any]) -> None:
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            return
        future = self._pending.pop(message_id, None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message.get("error")
            error = error if isinstance(error, dict) else {}
            future.set_exception(
                AgyError(str(error.get("message") or "agy refused the request"), error.get("code"))
            )
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AgyExited())
        self._pending.clear()


async def _ended(process: asyncio.subprocess.Process) -> None:
    """Wait for a child whose input was closed, signalling it only when it will not go."""
    try:
        await asyncio.wait_for(process.wait(), timeout=EXIT_GRACE)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=EXIT_GRACE)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
