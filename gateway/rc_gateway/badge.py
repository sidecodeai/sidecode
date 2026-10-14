"""Amendment A47: the app icon's count, and the badge-only push that keeps a phone's in step.

Every push carries the account's count (§3.7), so a phone's home-screen badge is right after any
notification it shows. The count also moves when nothing is pushed: a mark cleared by
`session.seen` or by the session working again, a marked session archived or removed, or a mark
set while an app watched the session and its push was suppressed. Each of those reaches the push
service as a `BadgeChange`, and once an account's count has been still for three seconds its phones
get one notification that changes the badge and shows nothing. Waiting for the count to settle is
what keeps a turn that ends on screen, and is opened a moment later, from costing a push at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from .logging import logger

log = logger("rc_gateway.badge")

#: §3.7: how long an account's count has to be still before its phones are told.
BADGE_SETTLE_SECONDS = 3.0
#: How long a shutdown waits for the counts it sends on its way out.
BADGE_DRAIN_SECONDS = 5.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class BadgeChange:
    """One account's count moved: a session's mark came or went, or a marked session left."""

    #: The account whose phones are told (A24).
    owner: str
    #: The session whose mark changed, which the badge-only payload names (§3.7).
    device_id: str
    session_id: str


#: The hub reports a change and moves on; nothing it calls may make the device's frame wait.
BadgeHook = Callable[[BadgeChange], None]
BadgeSender = Callable[[BadgeChange], Awaitable[None]]


class BadgeSettler:
    """Waits for each account's count to stop moving, then hands the newest change on once.

    ``sleep`` is how the quiet period is waited out; a test passes a clock it moves itself.
    """

    def __init__(
        self,
        send: BadgeSender,
        *,
        sleep: Sleep = asyncio.sleep,
        delay: float = BADGE_SETTLE_SECONDS,
    ) -> None:
        self._send = send
        self._sleep = sleep
        self._delay = delay
        self._latest: dict[str, BadgeChange] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def changed(self, change: BadgeChange) -> None:
        """Start the account's quiet period over; the change that ends it is the one sent."""
        timer = self._timers.pop(change.owner, None)
        if timer is not None:
            timer.cancel()
        self._latest[change.owner] = change
        self._timers[change.owner] = self._spawn(self._settle(change.owner))

    async def stop(self) -> None:
        """Send every count still settling now, rather than leave a phone's badge behind."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        waiting = list(self._latest.values())
        self._latest.clear()
        for change in waiting:
            self._spawn(self._deliver(change))
        if self._tasks:
            _, unfinished = await asyncio.wait(list(self._tasks), timeout=BADGE_DRAIN_SECONDS)
            for task in unfinished:
                task.cancel()

    async def _settle(self, owner: str) -> None:
        try:
            await self._sleep(self._delay)
        except asyncio.CancelledError:
            return
        # Off the timer table before the send, so a change arriving mid-send starts a quiet period
        # of its own instead of cancelling a delivery that is already under way.
        self._timers.pop(owner, None)
        change = self._latest.pop(owner, None)
        if change is not None:
            await self._deliver(change)

    async def _deliver(self, change: BadgeChange) -> None:
        try:
            await self._send(change)
        except Exception:
            log.exception("badge push failed")

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
