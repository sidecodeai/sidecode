"""Letting go of a Claude or pi process the device holds and nobody is using (A58).

A session an app launched, or one an app's message brought out of the Archive,
stays the device's until the person closes it, across restarts and updates too;
its process need not run all that time. One that has had nothing to do for 30
minutes is stopped: no turn running or starting, no approval or question open, no
message queued, no subagent or background task of its own still at work. The row
stays `remote` and `idle`, and the next message starts the process again. Codex
and Grok sessions live in a daemon or leader the device does not own, so there is
nothing of theirs to stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from ..logging_setup import logger

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry, SessionHub

log = logger("rc_client.idle")

# How long a process the device holds may sit unused before it is stopped.
IDLE_RELEASE = 30 * 60.0
# How often the sessions are looked at for one.
CHECK_EVERY = 60.0


class IdleRelease:
    """Looks for processes nobody has used for `IDLE_RELEASE`, every `CHECK_EVERY`."""

    def __init__(self, hub: SessionHub) -> None:
        self.hub = hub
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="idle-release")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("letting go of idle processes failed")

    async def sweep(self, now: float | None = None) -> int:
        """Stop every process that has been unused long enough; how many were stopped."""
        moment = time.monotonic() if now is None else now
        stopped = 0
        for entry in list(self.hub.entries.values()):
            if moment - entry.active_at < IDLE_RELEASE or not self.hub.own_process(entry):
                continue
            if await self.hub.release_process(entry):
                stopped += 1
                log.info("stopped the process of a session idle for 30 minutes")
        return stopped


def unused(entry: SessionEntry) -> bool:
    """Whether nothing is going on in a session the device drives: it can be let go of."""
    runner = entry.runner
    session = entry.session
    return (
        runner is not None
        and not runner.busy
        and session.turn is None
        and session.state == "idle"
        and not entry.queue
        and not entry.gate.locked()
    )
