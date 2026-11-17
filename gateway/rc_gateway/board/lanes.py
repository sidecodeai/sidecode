"""When each board is updated: one update at a time per session, spaced and debounced (A62).

A request for an update starts one now, or at the end of `MIN_INTERVAL_SECONDS` after the
session's last model call; while one is waiting, an earlier request brings it forward and a later
one joins it. A request that arrives while an update runs is kept, and one more update follows.
A turn's end asks for an update `TURN_END_DEBOUNCE_SECONDS` later, unless the next turn starts
first. While anything is waiting or running for a session, its board reads `updating`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from ..logging import logger
from .limits import MIN_INTERVAL_SECONDS

log = logger("rc_gateway.board")

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Request:
    #: What asked: `open`, `refresh`, `rebuild`, `turn_end`, `device_online` or `preference`.
    trigger: str
    #: Run inside a back-off, as an app's Refresh does.
    force: bool = False
    #: Start the board over first.
    rebuild: bool = False

    def joined(self, later: Request) -> Request:
        """One request for both: the first one's trigger unless the later one starts over."""
        trigger = later.trigger if later.rebuild and not self.rebuild else self.trigger
        return replace(
            self,
            trigger=trigger,
            force=self.force or later.force,
            rebuild=self.rebuild or later.rebuild,
        )


Run = Callable[[str, Request], Awaitable[None]]


@dataclass
class _Lane:
    waiting: asyncio.Task[None] | None = None
    due: float = 0.0
    pending: Request | None = None
    running: asyncio.Task[None] | None = None
    again: Request | None = None
    debounce: asyncio.Task[None] | None = None
    #: When the session's last model call started, by the service's clock.
    last_call: float | None = None

    def busy(self) -> bool:
        return any(task is not None for task in (self.waiting, self.running, self.debounce))

    def tasks(self) -> list[asyncio.Task[None]]:
        return [task for task in (self.waiting, self.running, self.debounce) if task is not None]


class Lanes:
    def __init__(self, run: Run, *, clock: Callable[[], float], sleep: Sleep) -> None:
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._lanes: dict[str, _Lane] = {}

    def busy(self, session_id: str) -> bool:
        lane = self._lanes.get(session_id)
        return lane is not None and lane.busy()

    def request(self, session_id: str, request: Request) -> None:
        lane = self._lanes.setdefault(session_id, _Lane())
        if lane.running is not None:
            lane.again = request if lane.again is None else lane.again.joined(request)
            return
        due = self._clock()
        if lane.last_call is not None:
            due = max(due, lane.last_call + MIN_INTERVAL_SECONDS)
        lane.pending = request if lane.pending is None else lane.pending.joined(request)
        if lane.waiting is not None:
            if lane.due <= due:
                return
            lane.waiting.cancel()
        lane.due = due
        lane.waiting = asyncio.create_task(self._start_at(session_id, lane, due))

    def debounce(self, session_id: str, seconds: float, request: Request) -> None:
        """Ask for an update ``seconds`` from now, replacing one asked for the same way before."""
        lane = self._lanes.setdefault(session_id, _Lane())
        if lane.debounce is not None:
            lane.debounce.cancel()
        lane.debounce = asyncio.create_task(self._after(session_id, lane, seconds, request))

    def cancel_debounce(self, session_id: str) -> None:
        lane = self._lanes.get(session_id)
        if lane is not None and lane.debounce is not None:
            lane.debounce.cancel()
            lane.debounce = None

    def called(self, session_id: str) -> None:
        """The session's update is calling the model now: the next call waits the interval."""
        self._lanes.setdefault(session_id, _Lane()).last_call = self._clock()

    def drop(self, session_id: str) -> None:
        lane = self._lanes.pop(session_id, None)
        if lane is not None:
            for task in lane.tasks():
                task.cancel()

    def prune(self) -> None:
        """Forget the lanes with nothing to do and no interval left to keep."""
        now = self._clock()
        for session_id, lane in list(self._lanes.items()):
            spaced = lane.last_call is not None and now - lane.last_call < MIN_INTERVAL_SECONDS
            if not lane.busy() and not spaced:
                del self._lanes[session_id]

    async def stop(self) -> None:
        tasks = [task for lane in self._lanes.values() for task in lane.tasks()]
        self._lanes.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _after(self, session_id: str, lane: _Lane, seconds: float, request: Request) -> None:
        await self._sleep(seconds)
        lane.debounce = None
        if self._lanes.get(session_id) is lane:
            self.request(session_id, request)

    async def _start_at(self, session_id: str, lane: _Lane, due: float) -> None:
        delay = due - self._clock()
        if delay > 0:
            await self._sleep(delay)
        request, lane.pending, lane.waiting = lane.pending, None, None
        if request is None:
            return
        lane.running = asyncio.current_task()
        try:
            await self._run(session_id, request)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("board update failed", session_id=session_id)
        finally:
            lane.running = None
        again, lane.again = lane.again, None
        if again is not None and self._lanes.get(session_id) is lane:
            self.request(session_id, again)
