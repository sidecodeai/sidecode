"""Waiting for one process to end, as the kernel says it.

A terminal CLI that quits takes a moment to do it: it flushes its transcript and
runs its own hooks after its channel bridge has already gone. One look at the
process table a second later found it still there, and the session then stayed
the terminal's until the next scan. The kernel says when a process ends: kqueue's
`NOTE_EXIT` on macOS, a pidfd that turns readable on Linux. Both are watched
from the event loop, so a wait costs neither a thread nor a helper process.
Where neither is available, the pid is looked at every 100 ms.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import select
from collections.abc import Callable
from typing import Any

POLL = 0.1


def alive(pid: int) -> bool:
    """Whether a process with this pid exists; one owned by somebody else counts."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def ended(pid: int, timeout: float) -> bool:
    """Whether process `pid` ends within `timeout` seconds, answered as soon as it does."""
    if not alive(pid):
        return True
    watch = _watch(pid)
    if watch is None:
        return await _poll(pid, timeout)
    fd, release = watch
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def readable() -> None:
        if not done.done():
            done.set_result(None)

    loop.add_reader(fd, readable)
    try:
        # A process that ended between the first look and the watch never says so.
        if not alive(pid):
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(done), timeout)
        return done.done() or not alive(pid)
    finally:
        loop.remove_reader(fd)
        release()


def _watch(pid: int) -> tuple[int, Callable[[], None]] | None:
    """A descriptor that turns readable when `pid` ends, and how to let it go; None if none.

    Each platform's call is looked up rather than named, so a type check on
    either platform reads the other one's code too.
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is not None:
        return _pidfd(pidfd_open, pid)
    if getattr(select, "kqueue", None) is not None:
        return _kqueue(pid)
    return None


def _pidfd(pidfd_open: Callable[[int], int], pid: int) -> tuple[int, Callable[[], None]] | None:
    try:
        fd = pidfd_open(pid)
    except OSError:
        return None
    return fd, lambda: os.close(fd)


def _kqueue(pid: int) -> tuple[int, Callable[[], None]] | None:
    # Read as `Any`: these names exist on macOS alone, and a type check runs on both.
    kernel: Any = select
    queue = kernel.kqueue()
    exit_event = kernel.kevent(
        pid,
        filter=kernel.KQ_FILTER_PROC,
        flags=kernel.KQ_EV_ADD | kernel.KQ_EV_ONESHOT,
        fflags=kernel.KQ_NOTE_EXIT,
    )
    try:
        queue.control([exit_event], 0, 0)
    except OSError:
        queue.close()
        return None
    return int(queue.fileno()), queue.close


async def _poll(pid: int, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while alive(pid):
        left = deadline - loop.time()
        if left <= 0:
            return False
        await asyncio.sleep(min(POLL, left))
    return True
