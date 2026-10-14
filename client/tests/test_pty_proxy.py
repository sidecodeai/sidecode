"""Amendment A40: the pseudo-terminal the shim starts Claude Code inside."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rc_client.channel import paths, wire
from rc_client.channel.screen import Keyboard, Screen
from rc_client.sessions.attach import AttachServer
from rc_client.sessions.ptys import PtyLink
from rc_client.sessions.typist import HIGHLIGHT

from .test_attach_channel import RecordingSink, short_dir  # noqa: F401  (fixture)

# A stand-in for the CLI: it reports its window size whenever the size
# changes, draws two picker rows in colour, and exits with a status of its own
# once a line is typed into it.
CHILD = r"""
import os, signal, sys

def show(*_):
    size = os.get_terminal_size()
    sys.stdout.write("size %dx%d\r\n" % (size.columns, size.lines))
    sys.stdout.flush()

signal.signal(signal.SIGWINCH, show)
show()
sys.stdout.write("\x1b]0;title\x07\x1b[2J\x1b[1;1H\x1b[32m  1. Default\x1b[0m\r\n")
sys.stdout.write("\u276f 2. Opus\r\n")
sys.stdout.flush()
line = sys.stdin.readline()
sys.stdout.write("typed %s\r\n" % line.strip())
sys.stdout.flush()
raise SystemExit(3)
"""


def test_the_screen_keeps_the_text_and_drops_the_escapes() -> None:
    screen = Screen()
    screen.feed(f"\x1b[2J\x1b[1;1H\x1b[32mhello\x1b[0m\r\n{HIGHLIGHT} 2. Opus\r\n".encode())
    screen.feed(f"{HIGHLIGHT} 3. Fable\r\n".encode())
    text = screen.text()
    assert "hello" in text
    assert f"{HIGHLIGHT} 3. Fable" in text
    assert "\x1b" not in text and "[32m" not in text


def test_an_osc_title_and_a_split_sequence_never_reach_the_text() -> None:
    screen = Screen()
    screen.feed(b"\x1b]0;a window title\x07ready")
    # One escape sequence, delivered in three reads.
    screen.feed(b"\x1b")
    screen.feed(b"[1;31")
    screen.feed(b"mred")
    assert screen.text().replace("\n", "") == "readyred"


def test_the_screen_keeps_only_its_tail() -> None:
    screen = Screen(limit=64)
    screen.feed(b"x" * 500)
    assert len(screen.text()) == 64


def test_the_keyboard_counts_a_draft_and_forgets_it_on_enter() -> None:
    keys = Keyboard()
    assert keys.draft == 0
    keys.feed(b"hello")
    assert keys.draft == 5
    keys.feed(b"\x7f")
    assert keys.draft == 4
    keys.feed(b"\r")
    assert keys.draft == 0
    keys.feed(b"again")
    keys.feed(b"\x15")
    assert keys.draft == 0
    keys.feed(b"more\x1b")
    assert keys.draft == 0
    assert keys.idle_for < 1.0


class Terminal:
    """A terminal for the proxy to live in, read in a thread so nothing blocks.

    Closing the two ends in order matters: the reader is woken by the slave
    closing, and only then is the master safe to close — the other way round
    leaves a thread stuck in a read on a pseudo-terminal that no longer exists.
    """

    def __init__(self, columns: int = 100, lines: int = 24) -> None:
        self.primary, self.secondary = pty.openpty()
        self.resize(columns, lines)
        self.text = ""
        self._reader = threading.Thread(target=self._run, daemon=True)
        self._reader.start()

    def resize(self, columns: int, lines: int) -> None:
        fcntl.ioctl(self.secondary, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))

    def type(self, data: bytes) -> None:
        """What the person at the keyboard sends."""
        os.write(self.primary, data)

    def _run(self) -> None:
        while True:
            try:
                chunk = os.read(self.primary, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.text += chunk.decode("utf-8", "replace")

    async def until(self, needle: str, timeout: float = 5.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if needle in self.text:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{needle!r} never reached the terminal: {self.text!r}")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.secondary)
        self._reader.join(timeout=5)
        with contextlib.suppress(OSError):
            os.close(self.primary)


@pytest.fixture
def terminal() -> Iterator[Terminal]:
    built = Terminal()
    try:
        yield built
    finally:
        built.close()


def _socket_in(home: Path) -> Path:
    """Where the proxy will dial, given `RC_CLIENT_HOME`."""
    return home / "state" / paths.SOCKET_NAME


def _spawn(terminal: Terminal, home: Path, script: str) -> subprocess.Popen[bytes]:
    """The proxy, running one throwaway script as if it were the CLI."""
    return subprocess.Popen(
        [sys.executable, "-m", paths.pty_module(), "--", sys.executable, "-c", script],
        stdin=terminal.secondary,
        stdout=terminal.secondary,
        stderr=terminal.secondary,
        env={**os.environ, "RC_CLIENT_HOME": str(home)},
    )


async def _stop(proxy: subprocess.Popen[bytes], server: AttachServer) -> None:
    if proxy.poll() is None:
        proxy.kill()
    await asyncio.to_thread(proxy.wait, 5)
    await server.stop()


async def _until(ready: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return
        await asyncio.sleep(0.05)


async def _link_for(server: AttachServer, timeout: float = 5.0) -> PtyLink:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        pids = server.ptys.pids()
        if pids:
            link = server.ptys.get(pids[0])
            assert link is not None
            return link
        await asyncio.sleep(0.05)
    raise AssertionError("the pseudo-terminal proxy never registered")


async def test_the_proxy_relays_a_real_terminal_and_answers_the_daemon(
    short_dir: Path,  # noqa: F811
    terminal: Terminal,
) -> None:
    """One run of the proxy, end to end: registration, keys, screen, exit status."""
    server = AttachServer(_socket_in(short_dir), RecordingSink())
    await server.start()
    proxy = _spawn(terminal, short_dir, CHILD)
    try:
        link = await _link_for(server)
        # The CLI runs in the proxy, not as the proxy: a pid of its own.
        assert link.pid != proxy.pid

        # The window size was copied before the CLI could ask for it.
        await terminal.until("size 100x24")

        state = await link.screen()
        assert "1. Default" in state.text
        assert f"{HIGHLIGHT} 2. Opus" in state.text
        assert "\x1b" not in state.text
        assert "a window title" not in state.text
        assert state.draft == 0

        # The person types, and the count follows without the text leaving.
        terminal.type(b"ab")
        await asyncio.sleep(0.3)
        assert (await link.state()).draft == 2
        terminal.type(b"\x15")
        await asyncio.sleep(0.3)
        assert (await link.state()).draft == 0

        # A resize reaches the CLI as its own SIGWINCH.
        terminal.resize(120, 30)
        proxy.send_signal(signal.SIGWINCH)
        await terminal.until("size 120x30")

        # And the device types, which ends the child.
        typed = await link.keys("done\r")
        assert typed.draft == 0
        await terminal.until("typed done")
        assert await asyncio.to_thread(proxy.wait, 10) == 3
    finally:
        await _stop(proxy, server)


async def test_a_terminal_that_goes_away_is_forgotten(
    short_dir: Path,  # noqa: F811
    terminal: Terminal,
) -> None:
    server = AttachServer(_socket_in(short_dir), RecordingSink())
    await server.start()
    proxy = _spawn(terminal, short_dir, "import sys; sys.stdin.readline()")
    try:
        link = await _link_for(server)
        await link.keys("\r")
        assert await asyncio.to_thread(proxy.wait, 10) == 0
        await _until(lambda: not server.ptys.pids())
        assert server.ptys.pids() == []
        assert server.ptys.get(link.pid) is None
    finally:
        await _stop(proxy, server)


async def test_the_daemon_stops_while_a_terminal_is_still_attached(
    short_dir: Path,  # noqa: F811
    terminal: Terminal,
) -> None:
    """Shutting down must not wait for a terminal somebody is still using."""
    server = AttachServer(_socket_in(short_dir), RecordingSink())
    await server.start()
    proxy = _spawn(terminal, short_dir, "import sys; sys.stdin.readline()")
    try:
        await _link_for(server)
        await asyncio.wait_for(server.stop(), timeout=5)
        assert server.ptys.pids() == []
    finally:
        if proxy.poll() is None:
            proxy.kill()
        await asyncio.to_thread(proxy.wait, 5)


def test_the_registration_frame_names_the_cli_and_nothing_else() -> None:
    assert wire.pty_register(1234) == {"type": "pty", "pid": 1234}
    assert wire.keys("r1", "\x1b[A") == {"type": "keys", "id": "r1", "data": "\x1b[A"}
    assert wire.keys("r1", "/model\r", clear=True)["clear"] is True
    assert wire.typed("r1", 3, 1.23456) == {
        "type": "typed",
        "id": "r1",
        "draft": 3,
        "idle_for": 1.235,
    }
    assert wire.screen_request("r2") == {"type": "screen", "id": "r2"}
    assert wire.state_request("r3") == {"type": "state", "id": "r3"}
