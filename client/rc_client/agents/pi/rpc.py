"""JSONL over stdin and stdout against `pi --mode rpc`.

Every command carries an `id` and is answered by exactly one `{"type":
"response"}` line; everything else on stdout is an event. Events go through a
queue to a single worker rather than being handled in the reader itself: they
carry streaming deltas, so they must stay in order, and a handler may send a
command of its own — asking pi for the session's totals when a turn ends, for
one — which only the reader can answer.

pi's framing rule is that LF is the *only* record delimiter, because U+2028 and
U+2029 are valid inside its JSON strings. `StreamReader.readline` splits on the
byte 0x0A alone, which no other character's UTF-8 encoding contains, so reading
the stream as bytes is compliant; decoding it into text first would not be.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ...errors import RcError
from ...logging_setup import logger

log = logger("rc_client.pi.rpc")

REQUEST_TIMEOUT = 30.0
# `abort` answers only once the session is idle, which can take a tool call.
DRAIN_TIMEOUT = 60.0
MAX_LINE_BYTES = 32 * 1024 * 1024

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
ClosedHandler = Callable[[], Awaitable[None]]


class PiProcess:
    """One `pi --mode rpc` child process."""

    def __init__(
        self,
        binary: str,
        *,
        cwd: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        on_event: EventHandler | None = None,
        on_closed: ClosedHandler | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = cwd
        self._args = list(args or [])
        self._env = env
        self._on_event = on_event
        self._on_closed = on_closed
        self._closing = False
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._binary,
            "--mode",
            "rpc",
            *self._args,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._env,
            limit=MAX_LINE_BYTES,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._worker = asyncio.create_task(self._event_loop())

    async def close(self) -> None:
        self._closing = True
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        for task in (self._reader, self._worker):
            if task is not None:
                task.cancel()
        self._reader = None
        self._worker = None
        process = self._process
        if process is not None and process.returncode is None:
            # Closing stdin is how pi is asked to stop; signalling a child that
            # has already exited raises rather than returning.
            with contextlib.suppress(ProcessLookupError, BrokenPipeError):
                if process.stdin is not None:
                    process.stdin.close()
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        self._process = None

    # ---------------------------------------------------------------- writes

    async def command(
        self, command: str, timeout: float | None = REQUEST_TIMEOUT, **params: Any
    ) -> dict[str, Any]:
        """Send one command and answer with its `data`, or raise what it refused."""
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise RcError("agent_unavailable", "pi is not running")
        request_id = f"rc-{self._next_id}"
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "type": command, **params}
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await process.stdin.drain()
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RcError("timeout", f"pi {command} timed out") from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RcError("agent_unavailable", "pi closed its input") from exc
        finally:
            self._pending.pop(request_id, None)
        if not response.get("success"):
            raise RcError("bad_request", str(response.get("error") or f"pi refused {command}"))
        data = response.get("data")
        return data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- reads

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                await self._handle_line(line)
        except asyncio.CancelledError:
            self._fail_pending()
            raise
        except Exception:
            log.exception("pi reader failed")
        self._fail_pending()
        if self._closing or self._on_closed is None:
            return
        # pi left on its own. Everything it already sent is published first, so
        # a turn that was running ends after its own last words.
        await self._events.join()
        await self._on_closed()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RcError("agent_unavailable", "pi exited"))
        self._pending.clear()

    async def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line.rstrip(b"\r\n"))
        except json.JSONDecodeError:
            log.warning("dropping malformed pi line", bytes=len(line))
            return
        if not isinstance(message, dict):
            return
        if message.get("type") == "response":
            self._answer(message)
            return
        if self._on_event is not None:
            await self._events.put(message)

    async def _event_loop(self) -> None:
        """One handler at a time, in the order pi sent them."""
        assert self._on_event is not None
        while True:
            event = await self._events.get()
            try:
                await self._on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pi event handler failed", event=str(event.get("type") or ""))
            finally:
                self._events.task_done()

    def _answer(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending.pop(str(request_id), None) if request_id else None
        if future is None:
            # A parse error has no id, and a command whose caller gave up has no
            # future left; neither can be delivered to anyone.
            if not message.get("success"):
                log.warning("pi refused a command", command=str(message.get("command") or ""))
            return
        if not future.done():
            future.set_result(message)
