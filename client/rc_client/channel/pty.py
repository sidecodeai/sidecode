"""`python -m rc_client.channel.pty`: the terminal the device can type into (A40).

The shim starts an attachable Claude Code inside this proxy. It is a
pseudo-terminal and nothing else: bytes go from the real terminal to the CLI
and back byte for byte, the window size follows, signals are passed on, and the
exit status is the CLI's. The person notices it only by what it makes possible
— a phone changing the model, the effort or compacting the context, typed into
the terminal exactly as they would type it themselves.

Two rules shape the file. **The terminal never waits for the daemon**: the
socket is dialled with backoff on the same loop that relays bytes, and every
frame is best-effort, so a daemon that is restarting, gone or wedged costs the
person nothing. **Nothing heavy is imported**: this process runs before the CLI
does, so the module-level imports are stdlib only and the socket path — which
needs the device's configuration — is read after the CLI has already started.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from typing import Any

from . import wire
from .screen import Keyboard, Screen

READ_BYTES = 64 * 1024
# The daemon may be down, restarting or not installed; none of that is the
# terminal's problem, so reconnection is slow and silent.
RECONNECT_FIRST = 0.5
RECONNECT_MAX = 30.0
SOCKET_TIMEOUT = 2.0
# Without the daemon there is nothing to wake up for, so the loop only needs a
# timeout fine enough to retry the connection on time.
IDLE_TIMEOUT = 0.5
EXEC_FAILED = 127

_FORWARDED = frozenset({int(signal.SIGTERM), int(signal.SIGHUP), int(signal.SIGINT)})


def _noop(signum: int, frame: Any) -> None:
    """Installed so the signal reaches the loop through the wakeup pipe."""


def _write_all(fd: int, data: bytes) -> bool:
    """Write every byte, or report that the other end is gone."""
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 0.05)
            continue
        except OSError:
            return False
        view = view[written:]
    return True


def _window_size(fd: int) -> bytes | None:
    try:
        return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    except OSError:
        return None


def _copy_window(source: int, target: int) -> None:
    size = _window_size(source)
    if size is None:
        return
    with contextlib.suppress(OSError):
        fcntl.ioctl(target, termios.TIOCSWINSZ, size)


class DaemonLink:
    """The daemon end of the proxy: one frame per line, never blocking for long."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._next_attempt = 0.0
        self._backoff = RECONNECT_FIRST

    @property
    def fileno(self) -> int | None:
        return self._socket.fileno() if self._socket is not None else None

    def due_in(self) -> float:
        """Seconds until the next dial; the loop uses it as its select timeout."""
        if self._socket is not None:
            return IDLE_TIMEOUT
        return max(0.0, self._next_attempt - time.monotonic())

    def dial(self) -> None:
        """Try once, if it is time, and say who we are when it works."""
        if self._socket is not None or time.monotonic() < self._next_attempt:
            return
        self._next_attempt = time.monotonic() + self._backoff
        self._backoff = min(RECONNECT_MAX, self._backoff * 2)
        path = _socket_path()
        if path is None:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(SOCKET_TIMEOUT)
            sock.connect(path)
        except OSError:
            return
        self._socket = sock
        self._buffer = b""
        if self.send(wire.pty_register(self._pid)):
            self._backoff = RECONNECT_FIRST

    def send(self, frame: dict[str, Any]) -> bool:
        sock = self._socket
        if sock is None:
            return False
        try:
            sock.sendall(wire.encode(frame))
        except OSError:
            self.close()
            return False
        return True

    def receive(self) -> list[dict[str, Any]]:
        """Whatever whole frames have arrived; an empty read closes the link."""
        sock = self._socket
        if sock is None:
            return []
        try:
            chunk = sock.recv(READ_BYTES)
        except (TimeoutError, BlockingIOError):
            return []
        except OSError:
            chunk = b""
        if not chunk:
            self.close()
            return []
        self._buffer += chunk
        if len(self._buffer) > wire.MAX_LINE_BYTES:
            self.close()
            return []
        frames: list[dict[str, Any]] = []
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            message = wire.decode(line)
            if message is not None:
                frames.append(message)
        return frames

    def close(self) -> None:
        sock, self._socket = self._socket, None
        self._buffer = b""
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


def _socket_path() -> str | None:
    """Where the daemon listens, read late so the terminal starts at once.

    `rc_client.channel.paths` reads the device's configuration, which costs
    more to import than everything else in this process put together.
    """
    try:
        from .paths import socket_path

        return str(socket_path())
    except Exception:
        return None


class PtyProxy:
    """One CLI in a pseudo-terminal, relayed to the real one."""

    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.child = 0
        self.master = -1
        self.screen = Screen()
        self.keyboard = Keyboard()
        self._stdin = sys.stdin.fileno()
        self._stdout = sys.stdout.fileno()
        self._saved: list[Any] | None = None
        self._wake_read = -1
        self._wake_write = -1
        self._reading_stdin = True

    # ----------------------------------------------------------------- setup

    def spawn(self) -> None:
        """Start the CLI on a terminal of its own, sized like the real one."""
        master, slave = os.openpty()
        self._copy_terminal(slave)
        _copy_window(self._stdin, master)
        pid = os.fork()
        if pid == 0:
            self._become_child(master, slave)
        os.close(slave)
        self.master = master
        self.child = pid

    def _copy_terminal(self, slave: int) -> None:
        """Give the CLI the same line discipline the person's terminal has."""
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(self._stdin))

    def _become_child(self, master: int, slave: int) -> None:
        """Never returns: this is the forked process, on its way to the CLI."""
        try:
            os.close(master)
            os.setsid()
            with contextlib.suppress(OSError):
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for target in (0, 1, 2):
                os.dup2(slave, target)
            if slave > 2:
                os.close(slave)
            os.execvp(self.command[0], self.command)
        except BaseException:
            os._exit(EXEC_FAILED)
        os._exit(EXEC_FAILED)

    def raw_mode(self) -> None:
        try:
            self._saved = termios.tcgetattr(self._stdin)
            tty.setraw(self._stdin)
        except (termios.error, OSError):
            self._saved = None

    def restore(self) -> None:
        if self._saved is None:
            return
        with contextlib.suppress(termios.error, OSError):
            termios.tcsetattr(self._stdin, termios.TCSADRAIN, self._saved)
        self._saved = None

    def catch_signals(self) -> None:
        """Route signals through a pipe, so the relay loop sees them at once."""
        self._wake_read, self._wake_write = os.pipe()
        for fd in (self._wake_read, self._wake_write):
            os.set_blocking(fd, False)
        for number in (int(signal.SIGWINCH), *_FORWARDED):
            with contextlib.suppress(OSError, ValueError):
                signal.signal(number, _noop)
        with contextlib.suppress(OSError, ValueError):
            signal.set_wakeup_fd(self._wake_write)

    # ------------------------------------------------------------------ loop

    def run(self) -> int:
        link = DaemonLink(self.child)
        while True:
            link.dial()
            sources = [self.master, self._wake_read]
            if self._reading_stdin:
                sources.append(self._stdin)
            link_fd = link.fileno
            if link_fd is not None:
                sources.append(link_fd)
            try:
                ready, _, _ = select.select(sources, [], [], link.due_in())
            except InterruptedError:
                continue
            except OSError:
                break
            if self._wake_read in ready:
                self._signals()
            if self._stdin in ready and not self._from_keyboard():
                self._reading_stdin = False
            if link_fd is not None and link_fd in ready:
                for message in link.receive():
                    self._answer(link, message)
            if self.master in ready and not self._from_child():
                break
        link.close()
        return self._reap()

    def _signals(self) -> None:
        try:
            numbers = os.read(self._wake_read, 512)
        except OSError:
            return
        for number in numbers:
            if number == signal.SIGWINCH:
                _copy_window(self._stdin, self.master)
            elif number in _FORWARDED:
                with contextlib.suppress(OSError):
                    os.kill(self.child, number)

    def _from_keyboard(self) -> bool:
        """The person typed: it goes to the CLI untouched, and is counted."""
        try:
            data = os.read(self._stdin, READ_BYTES)
        except OSError:
            return False
        if not data:
            return False
        self.keyboard.feed(data)
        return _write_all(self.master, data)

    def _from_child(self) -> bool:
        """The CLI printed: it goes to the real terminal, and is remembered."""
        try:
            data = os.read(self.master, READ_BYTES)
        except OSError:
            # A pseudo-terminal whose last slave closed reads as EIO, not EOF.
            return False
        if not data:
            return False
        self.screen.feed(data)
        return _write_all(self._stdout, data)

    def _answer(self, link: DaemonLink, message: dict[str, Any]) -> None:
        """One request from the daemon, answered under the id it came with."""
        request_id = str(message.get("id") or "")
        kind = message.get("type")
        if not request_id:
            return
        draft, idle_for = self.keyboard.draft, self.keyboard.idle_for
        if kind == wire.KEYS:
            data = str(message.get("data") or "").encode("utf-8")
            if message.get("clear"):
                self.screen.reset()
            if data:
                _write_all(self.master, data)
            link.send(wire.typed(request_id, draft, idle_for))
        elif kind == wire.SCREEN:
            link.send(wire.screen(request_id, self.screen.text(), draft, idle_for))
        elif kind == wire.STATE:
            link.send(wire.state(request_id, draft, idle_for))

    def _reap(self) -> int:
        """The CLI's own exit status, which is this process's too."""
        try:
            _, status = os.waitpid(self.child, 0)
        except OSError:
            return 0
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return os.WEXITSTATUS(status)

    def close(self) -> None:
        self.restore()
        with contextlib.suppress(OSError, ValueError):
            signal.set_wakeup_fd(-1)
        for fd in (self.master, self._wake_read, self._wake_write):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


def _command(argv: list[str]) -> list[str]:
    """Everything after `--` is the CLI to run; there is no other argument."""
    if "--" in argv:
        return argv[argv.index("--") + 1 :]
    return argv


def main(argv: list[str] | None = None) -> int:
    command = _command(list(sys.argv[1:] if argv is None else argv))
    if not command:
        print("rc-client pty: nothing to run", file=sys.stderr, flush=True)
        return 2
    proxy = PtyProxy(command)
    proxy.spawn()
    proxy.raw_mode()
    proxy.catch_signals()
    try:
        return proxy.run()
    finally:
        proxy.close()


if __name__ == "__main__":
    sys.exit(main())
