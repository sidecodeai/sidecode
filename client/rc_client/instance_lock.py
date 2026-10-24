"""One daemon per device home.

The daemon unlinks its channel and pi sockets before it binds them and again when
it stops, and `asyncio` removes a socket file that is in the way. A second daemon
on the same home would therefore delete the first one's sockets as it started or
as it exited, and every shim, hook, bridge and pi extension would dial a path
nobody listens on until the service restarted. So the daemon takes an exclusive
`flock` on `state/daemon.lock` before it touches any of that, and keeps it until
the sockets are gone.

The kernel drops the lock when the process ends, `SIGKILL` included, so a crash
leaves nothing to clean up. The descriptor is never inherited: a child that
outlived the daemon would otherwise keep the home locked. The file is never
removed, because a lock on a file somebody has since unlinked guards nothing.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from pathlib import Path

from .config import state_dir
from .errors import RcError

LOCK_FILE = "daemon.lock"
# A predecessor that was asked to stop may still be closing its sockets, so a
# refusal is retried for a while before it is believed.
WAIT_SECONDS = 20.0
POLL_SECONDS = 0.25


def lock_path() -> Path:
    return state_dir() / LOCK_FILE


class InstanceLock:
    """An exclusive claim on one device home, held until `release` or the process ends."""

    def __init__(self, target: Path) -> None:
        self.path = target
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock if nobody holds it. False means another process does."""
        if self.held:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Read-write, because an exclusive lock over NFS needs a descriptor that can write.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                return False
            raise
        self._fd = fd
        return True

    async def acquire(self) -> None:
        """Wait up to `WAIT_SECONDS` for the home, then refuse with a `conflict`."""
        deadline = time.monotonic() + WAIT_SECONDS
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise RcError(
                    "conflict",
                    f"another rc-client is already running for this home (it holds {self.path}); "
                    "stop it first, for example with `rc-client service stop`",
                )
            await asyncio.sleep(POLL_SECONDS)

    def release(self) -> None:
        """Let go of the home. Everything the lock guarded must be gone by now."""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)
