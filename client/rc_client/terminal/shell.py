"""One pseudo-terminal: the person's login shell, streamed to one app connection.

PROTOCOL.md 7.3 (A38). The shell runs as the user the client runs as, in the
home directory, with `TERM=xterm-256color`. Output is coalesced for about 16 ms
and published in frames of at most 16 KiB with a `seq` that rises by one; the
last 64 KiB is kept so an `attach` can redraw the screen. Nothing that travels
through a terminal is logged: only that one opened and closed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..child_env import sanitized_child_env
from ..logging_setup import logger

log = logger("rc_client.terminal")

Publish = Callable[[dict[str, Any]], Awaitable[None]]

COALESCE_SECONDS = 0.016
MAX_OUTPUT_BYTES = 16 * 1024
SCROLLBACK_BYTES = 64 * 1024
READ_BYTES = 64 * 1024
# `terminal.close` is a SIGHUP the shell may act on, then a SIGKILL it cannot.
CLOSE_GRACE_SECONDS = 2.0
REAP_TIMEOUT_SECONDS = 2.0
REAP_POLL_SECONDS = 0.01
SHUTDOWN_GRACE_SECONDS = 0.5


def login_shell() -> str:
    """The shell a terminal starts: `$SHELL`, else `/bin/sh` (7.3)."""
    return os.environ.get("SHELL") or "/bin/sh"


def window_size(cols: int, rows: int) -> bytes:
    return struct.pack("HHHH", rows, cols, 0, 0)


def set_window_size(fd: int, cols: int, rows: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, window_size(cols, rows))


def spawn_shell(cols: int, rows: int) -> tuple[int, int]:
    """Fork a login shell on a new pseudo-terminal; returns its pid and master fd.

    The child sets its own window size before `exec`, so the shell reads the
    app's size rather than the 0x0 a fresh pseudo-terminal starts with.
    """
    shell = login_shell()
    env = sanitized_child_env()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    home = os.path.expanduser("~")
    pid, master = pty.fork()
    if pid == 0:
        # The child: no asyncio, no logging, nothing but the exec.
        set_window_size(0, cols, rows)
        with contextlib.suppress(OSError):
            os.chdir(home)
        with contextlib.suppress(OSError):
            os.execvpe(shell, [shell, "-l"], env)
        os._exit(127)
    return pid, master


class Terminal:
    """One shell on a pseudo-terminal, owned by `TerminalManager`."""

    def __init__(
        self,
        *,
        holder: str,
        cols: int,
        rows: int,
        publish: Publish,
        on_gone: Callable[[Terminal], None],
    ) -> None:
        self.terminal_id = str(uuid.uuid4())
        self.cols = cols
        self.rows = rows
        # The connection output streams to, and the last one to hold it: an
        # `exited` frame is addressed to that one even after a detach, so a
        # reconnected app learns the shell is gone instead of waiting for it.
        self.holder: str | None = holder
        self._last_holder = holder
        self._publish = publish
        self._on_gone = on_gone
        self._seq = 0
        self._ring = bytearray()
        self._pending = bytearray()
        self._outgoing = bytearray()
        self._wake = asyncio.Event()
        self._eof = False
        self._reading = False
        self._writing = False
        self._closing = False
        self._done = False
        self._pid = 0
        self._fd = -1
        self._kill_timer: asyncio.TimerHandle | None = None
        self._keep_alive: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._pid, self._fd = spawn_shell(self.cols, self.rows)
        os.set_blocking(self._fd, False)
        loop = asyncio.get_running_loop()
        loop.add_reader(self._fd, self._readable)
        self._reading = True
        self._task = asyncio.create_task(self._run(), name=f"terminal-{self.terminal_id}")

    async def _run(self) -> None:
        """Coalesce what the shell writes, publish it, then report the exit."""
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                if self._pending:
                    await asyncio.sleep(COALESCE_SECONDS)
                    await self._flush()
                if self._eof and not self._pending:
                    break
            await self._exited(await self._reap())
        finally:
            self._cleanup()

    async def shutdown(self) -> None:
        """End the shell because the daemon is stopping (7.3: none survive it)."""
        task = self._task
        self._signal(signal.SIGHUP)
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=SHUTDOWN_GRACE_SECONDS)
            if not done:
                self._signal(signal.SIGKILL)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await self._reap()
        self._cleanup()

    def _cleanup(self) -> None:
        """Release the pseudo-terminal and let the manager forget this id."""
        if self._done:
            return
        self._done = True
        self._cancel(self._keep_alive)
        self._keep_alive = None
        self._cancel(self._kill_timer)
        self._kill_timer = None
        self._unwatch()
        if self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = -1
        self._on_gone(self)

    @staticmethod
    def _cancel(timer: asyncio.TimerHandle | None) -> None:
        if timer is not None:
            timer.cancel()

    # ----------------------------------------------------------------- input

    def write(self, data: bytes) -> None:
        """Queue bytes for the shell and push as many as the terminal takes."""
        if self._done or self._fd < 0:
            return
        self._outgoing.extend(data)
        self._drain()

    def _drain(self) -> None:
        while self._outgoing and self._fd >= 0:
            try:
                written = os.write(self._fd, self._outgoing)
            except BlockingIOError:
                self._watch_writable(True)
                return
            except OSError:
                self._outgoing.clear()
                self._mark_eof()
                return
            if written <= 0:
                break
            del self._outgoing[:written]
        self._watch_writable(False)

    def _watch_writable(self, wanted: bool) -> None:
        if wanted == self._writing or self._fd < 0:
            return
        loop = asyncio.get_running_loop()
        if wanted:
            loop.add_writer(self._fd, self._drain)
        else:
            loop.remove_writer(self._fd)
        self._writing = wanted

    # ---------------------------------------------------------------- output

    def _readable(self) -> None:
        try:
            data = os.read(self._fd, READ_BYTES)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._mark_eof()
            return
        self._pending.extend(data)
        self._wake.set()

    def _mark_eof(self) -> None:
        self._eof = True
        self._unwatch()
        self._wake.set()

    def _unwatch(self) -> None:
        if self._fd < 0:
            return
        loop = asyncio.get_running_loop()
        if self._reading:
            loop.remove_reader(self._fd)
            self._reading = False
        if self._writing:
            loop.remove_writer(self._fd)
            self._writing = False

    async def _flush(self) -> None:
        data = bytes(self._pending)
        self._pending.clear()
        if not data:
            return
        self._remember(data)
        if self.holder is None:
            return
        for start in range(0, len(data), MAX_OUTPUT_BYTES):
            self._seq += 1
            await self._publish(
                {
                    "type": "terminal.output",
                    "terminal_id": self.terminal_id,
                    "to": self.holder,
                    "seq": self._seq,
                    "data": _b64(data[start : start + MAX_OUTPUT_BYTES]),
                }
            )

    def _remember(self, data: bytes) -> None:
        self._ring.extend(data)
        if len(self._ring) > SCROLLBACK_BYTES:
            del self._ring[: len(self._ring) - SCROLLBACK_BYTES]

    async def _exited(self, code: int | None) -> None:
        log.info("terminal ended", terminal=self.terminal_id)
        await self._publish(
            {
                "type": "terminal.exited",
                "terminal_id": self.terminal_id,
                "to": self.holder or self._last_holder,
                "code": code,
            }
        )

    # -------------------------------------------------------------- requests

    def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        if self._fd >= 0:
            set_window_size(self._fd, cols, rows)

    def attach(self, holder: str) -> dict[str, Any]:
        """Move the output to `holder` and hand back the screen it missed."""
        self._cancel(self._keep_alive)
        self._keep_alive = None
        self.holder = holder
        self._last_holder = holder
        return {
            "terminal_id": self.terminal_id,
            "cols": self.cols,
            "rows": self.rows,
            "scrollback": _b64(bytes(self._ring)),
        }

    def detach(self, keep_alive: float) -> None:
        """Stop streaming and keep the shell for `keep_alive` seconds (7.3)."""
        self.holder = None
        self._cancel(self._keep_alive)
        loop = asyncio.get_running_loop()
        self._keep_alive = loop.call_later(keep_alive, self.close)

    def close(self) -> None:
        """End the shell: SIGHUP, then SIGKILL after a grace. Idempotent."""
        if self._closing:
            return
        self._closing = True
        self._cancel(self._keep_alive)
        self._keep_alive = None
        self._signal(signal.SIGHUP)
        loop = asyncio.get_running_loop()
        self._kill_timer = loop.call_later(CLOSE_GRACE_SECONDS, self._signal, signal.SIGKILL)

    # ----------------------------------------------------------------- child

    def _signal(self, number: int) -> None:
        if self._pid <= 0:
            return
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(self._pid), number)

    async def _reap(self) -> int | None:
        """Collect the child without blocking the loop; SIGKILL if it lingers."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + REAP_TIMEOUT_SECONDS
        killed = False
        while True:
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
            except OSError:
                return None
            if pid == self._pid:
                try:
                    return os.waitstatus_to_exitcode(status)
                except ValueError:
                    return None
            if loop.time() >= deadline:
                if killed:
                    return None
                killed = True
                self._signal(signal.SIGKILL)
                deadline = loop.time() + REAP_TIMEOUT_SECONDS
            await asyncio.sleep(REAP_POLL_SECONDS)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
