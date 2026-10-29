"""Amendment A56: fetching from a device what the gateway has not seen.

A device keeps every event of its sessions; the gateway keeps a bounded tail in memory and nothing
on disk. After a restart, an evicted buffer or a link outage the gateway therefore lacks events an
app is waiting for, and the device is the one place that has them. This module holds what the hub
needs to ask for them without trusting the answer to be small, quick or in order: the bounded
request, the gate that keeps a device from being flooded, the hold that keeps a session's live
events behind the ones being fetched so an app never sees them out of order, and the catch-ups a
device's hello starts.

Nothing here touches the disk, and nothing here is billed: it is remote control, which every
account has.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol

from .connections import AppConnection, DeviceConnection, encode
from .frames import Frame, int_field, object_field, text_field
from .logging import logger
from .replay import ReplayBuffer

log = logger("rc_gateway.catchup")

#: What one request may take from a device: the events after a cursor, as many as the replay buffer
#: could hold per quarter, within a megabyte, and only as long as an app is willing to wait.
FETCH_EVENTS = 500
FETCH_BYTES = 1024 * 1024
FETCH_SECONDS = 3.0
#: A device's hello may find a longer gap, which is fetched page after page: this many pages, and
#: this many seconds for all of them, the wait for a turn included. A gap past either is answered
#: with `resync`, which pages history instead.
CATCH_UP_PAGES = 5
CATCH_UP_SECONDS = 10.0
#: What the catch-ups that are sending one socket events may give it at once, the frames its queue
#: holds already and the events held behind the fetch counted. An app's send queue takes 2048
#: frames and an app that overruns it loses its socket, so a socket that would be given more is
#: marked and sent none of the fetch, and pages history instead.
CATCH_UP_EVENTS = 1500
#: A device answers `session.history` from its own store, so it is the one to protect from a herd
#: of apps that reconnected together after a restart.
FETCHES_PER_DEVICE = 2
#: What a session's live events may add up to while its fetch is in flight. A fetch ends within
#: seconds, so this is a safety bound against a device that never stops talking, not a tuning knob.
HOLD_EVENTS = 1000
HOLD_BYTES = 4 * 1024 * 1024

#: What a subscribe or a catch-up reports having done, when it did anything worth a line.
OUTCOME_REPLAYED = "replayed"
OUTCOME_BACKFILLED = "device_backfilled"
OUTCOME_RESYNC = "resync"
OUTCOME_FETCH_FAILED = "fetch_failed"

#: The hub's own way of asking a device, with the time to wait for the answer.
Ask = Callable[[DeviceConnection, Frame, float], Awaitable[Frame | None]]


class Publish(Protocol):
    """The hub's way of buffering one event and fanning it out.

    ``known`` says the event was fetched rather than pushed: the newest ``seq`` the gateway had
    seen of the session before, so that only a socket behind the event is sent it. Sockets in
    ``skip`` are sent none of it.
    """

    async def __call__(
        self,
        device: DeviceConnection,
        session_id: str,
        event: Frame,
        frame: Frame,
        *,
        known: int | None = None,
        skip: frozenset[AppConnection] = frozenset(),
    ) -> bool: ...


async def fetch_missing(
    ask: Ask,
    device: DeviceConnection,
    session_id: str,
    after: int,
    timeout: float,
    *,
    pages: int = 1,
    budget: float | None = None,
) -> list[Frame] | None:
    """The events a device holds after ``after``, or ``None`` when they do not fit the bounds.

    ``session.history`` with ``after_seq`` is amendment A9's: the latest version of each block and
    the non-block events of §6.4, ascending. A gap the first page does not cover is fetched page
    after page, up to ``pages`` of them and ``budget`` seconds in all, and nothing is returned
    until the last page is in: delivering part of a gap is how a hole gets into a transcript. A
    reply that is an error, holds more than `FETCH_EVENTS` or adds up to more than `FETCH_BYTES`
    is no answer at all, and neither is a gap that outlasts the pages or the budget.
    """
    deadline = None if budget is None else time.monotonic() + budget
    kept: dict[int, Frame] = {}
    cursor = after
    for _ in range(pages):
        wait = timeout if deadline is None else min(timeout, deadline - time.monotonic())
        if wait <= 0:
            return None
        page = await _fetch_page(ask, device, session_id, cursor, wait)
        if page is None:
            return None
        events, has_more = page
        for event in events:
            kept[int_field(event, "seq") or 0] = event
        if not has_more:
            return [kept[seq] for seq in sorted(kept)]
        if not events:
            return None
        cursor = max(kept)
    return None


async def _fetch_page(
    ask: Ask, device: DeviceConnection, session_id: str, after: int, timeout: float
) -> tuple[list[Frame], bool] | None:
    request: Frame = {
        "type": "session.history",
        "session_id": session_id,
        "after_seq": after,
        "limit": FETCH_EVENTS,
    }
    reply = await ask(device, request, timeout)
    if reply is None or reply.get("ok") is not True:
        return None
    result = object_field(reply, "result") or {}
    raw = result.get("events")
    if not isinstance(raw, list) or len(raw) > FETCH_EVENTS:
        return None
    kept: dict[int, Frame] = {}
    size = 0
    for event in raw:
        seq = int_field(event, "seq") if isinstance(event, dict) else None
        if seq is None or seq <= after:
            continue
        size += len(encode(event).encode("utf-8"))
        if size > FETCH_BYTES:
            return None
        kept[seq] = event
    return [kept[seq] for seq in sorted(kept)], bool(result.get("has_more"))


@dataclass
class _Shared[T]:
    item: T
    users: int = 0


class FetchGate:
    """At most one fetch per session and `FETCHES_PER_DEVICE` per device at a time (A56).

    A fetch holds its session's lock before it takes a device slot, and a device slot is never held
    while waiting for a session's lock, so two fetches cannot wait for each other.
    """

    def __init__(self, per_device: int = FETCHES_PER_DEVICE) -> None:
        self._per_device = per_device
        self._sessions: dict[str, _Shared[asyncio.Lock]] = {}
        self._devices: dict[str, _Shared[asyncio.Semaphore]] = {}

    @asynccontextmanager
    async def slot(
        self, device_id: str, session_id: str, *, within: float | None = None
    ) -> AsyncIterator[None]:
        """Wait for a turn, for at most ``within`` seconds (``TimeoutError`` after that)."""
        lock = _join(self._sessions, session_id, asyncio.Lock)
        slots = _join(self._devices, device_id, lambda: asyncio.Semaphore(self._per_device))
        held: list[Callable[[], None]] = []
        try:
            async with asyncio.timeout(within):
                await lock.item.acquire()
                held.append(lock.item.release)
                await slots.item.acquire()
                held.append(slots.item.release)
            yield
        finally:
            for release in reversed(held):
                release()
            _leave(self._sessions, session_id, lock)
            _leave(self._devices, device_id, slots)


def _join[T](table: dict[str, _Shared[T]], key: str, make: Callable[[], T]) -> _Shared[T]:
    shared = table.get(key)
    if shared is None:
        shared = table[key] = _Shared(make())
    shared.users += 1
    return shared


def _leave[T](table: dict[str, _Shared[T]], key: str, shared: _Shared[T]) -> None:
    shared.users -= 1
    if shared.users == 0 and table.get(key) is shared:
        del table[key]


@dataclass(frozen=True)
class CatchUp:
    """One session to fetch the missing events of after a device hello."""

    session_id: str
    #: The account that owns the device, whose sockets are the only ones fetched for.
    owner: str
    #: The fetch starts after this: the lowest cursor among those waiting, or the replay buffer's
    #: end when that is lower.
    after: int
    #: Where the device says the session has got to.
    tip: int
    #: How far the gateway knew the session before this hello: only events past it cue a push.
    known: int
    #: The sockets that were waiting in the gap when it was planned.
    behind: tuple[AppConnection, ...]


@dataclass
class Hold:
    """A session's live events, kept back while the events before them are fetched (A56).

    An app drops an event at or below its cursor as late, so one that overtook the events a fetch
    is bringing would cost it those events for good. They are published with the fetched ones, in
    `seq` order, and the replay buffer turns away any the fetch already delivered.
    """

    device: DeviceConnection
    item: CatchUp
    #: The task doing the fetch, so a link that drops can end it.
    task: asyncio.Task[None] | None = None
    events: list[tuple[Frame, Frame]] = field(default_factory=list)
    size: int = 0
    #: Set when the hold filled up and was let go of: the fetch is no longer in front of what has
    #: been published since, so it is not published.
    overflowed: bool = False

    def add(self, event: Frame, frame: Frame) -> bool:
        """Keep one live event. False once the hold is full, and the caller lets go of it."""
        size = len(encode(frame))
        if len(self.events) >= HOLD_EVENTS or self.size + size > HOLD_BYTES:
            return False
        self.events.append((event, frame))
        self.size += size
        return True

    def take(self) -> list[tuple[Frame, Frame]]:
        """What has been kept since the last call, oldest `seq` first."""
        taken = sorted(self.events, key=lambda item: int_field(item[0], "seq") or 0)
        self.events, self.size = [], 0
        return taken


@dataclass(frozen=True)
class CatchUpHost:
    """What catch-ups need from the hub, as callables and shared maps, so a test can drive them."""

    ask: Ask
    publish: Publish
    apps: Callable[[], Iterable[AppConnection]]
    #: The hub's replay buffers by session, which a fetch fills and a failed one may drop.
    buffers: dict[str, ReplayBuffer]
    #: The device that owns a session, from memory: what a hello was just told about.
    session_owner: Callable[[str], str | None]
    #: The same from memory or, once the entry has been evicted, from the index.
    owner_of: Callable[[str], Awaitable[str | None]]
    #: The account that owns a device, from memory.
    device_account: Callable[[str], str]
    #: How long one request waits for its device.
    timeout: Callable[[], float]
    #: How long a fetch may take in all, the wait for its turn included.
    budget: Callable[[], float]


class CatchUps:
    """The fetches a device's hello starts, and the holds that keep live events behind them."""

    def __init__(self, host: CatchUpHost) -> None:
        self._host = host
        self._holds: dict[str, Hold] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._gate = FetchGate()

    # ---- at a device hello ----

    def start(
        self, connection: DeviceConnection, summaries: list[Frame], known: dict[str, int]
    ) -> None:
        """Fetch what a device has of each session an app is waiting on and the gateway lacks.

        A device keeps working while its link is down and across a gateway restart, so the events
        of the outage never reached the replay buffer, and an app that subscribed before this
        hello still waits at the cursor it had. Each session is held before the device's frames
        are read, so none of its live events can overtake the fetch.
        """
        for item in self._plan(connection, summaries, known):
            hold = self._holds[item.session_id] = Hold(connection, item)
            hold.task = asyncio.create_task(self._run(connection, item, hold))
            hold.task.add_done_callback(functools.partial(self._ended, hold))
            self._track(hold.task)

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ended(self, hold: Hold, task: asyncio.Task[None]) -> None:
        """A task cancelled before its first step never let go of its hold, so this does."""
        if self._holds.get(hold.item.session_id) is hold:
            self._track(asyncio.create_task(self._give_up(hold.item, hold)))

    def supersede(self, previous: DeviceConnection) -> None:
        """A newer connection of the device replaced ``previous``: the fetches it left are over.

        What they asked for will never arrive. What they held is dropped, neither published nor
        buffered: the new connection's hello fetches again from each subscriber's cursor, and the
        device still has every event history carries, sends Up next again after its handshake and
        its summaries in the hello. Whoever was behind is marked until then.
        """
        for session_id, hold in list(self._holds.items()):
            if hold.device is not previous:
                continue
            del self._holds[session_id]
            for app in self._behind(hold.item):
                app.mark_stale(session_id)
            if hold.task is not None:
                hold.task.cancel()

    async def _may_publish(self, device: DeviceConnection, session_id: str, events: int) -> bool:
        """Whether the device owns the session: only its own events are published into it (A24)."""
        if await self._host.owner_of(session_id) == device.device_id:
            return True
        log.warning(
            "events for a session the device does not own dropped",
            device_id=device.device_id,
            session_id=session_id,
            events=events,
        )
        return False

    def _intact(self, item: CatchUp, hold: Hold) -> bool:
        """Whether the hold is still its session's, and was not let go of for being full."""
        return not hold.overflowed and self._holds.get(item.session_id) is hold

    def _plan(
        self, connection: DeviceConnection, summaries: list[Frame], known: dict[str, int]
    ) -> list[CatchUp]:
        host = self._host
        owner = host.device_account(connection.device_id)
        waiting = self._waiting(owner)
        plan = []
        for session_id, tip in _session_tips(summaries).items():
            # Only the device that owns a session may have events published into it.
            if session_id in self._holds or host.session_owner(session_id) != connection.device_id:
                continue
            item = self._plan_one(
                session_id, owner, tip, known.get(session_id, tip), waiting.get(session_id, [])
            )
            if item is not None:
                plan.append(item)
        return plan

    def _plan_one(
        self,
        session_id: str,
        owner: str,
        tip: int,
        known: int,
        waiting: list[tuple[AppConnection, int]],
    ) -> CatchUp | None:
        behind = [(app, cursor) for app, cursor in waiting if cursor < tip]
        buffer = self._host.buffers.get(session_id)
        cursors = [cursor for _, cursor in behind]
        if buffer is not None and len(buffer) and buffer.last_seq < tip:
            cursors.append(buffer.last_seq)
        if not cursors:
            return None
        return CatchUp(
            session_id=session_id,
            owner=owner,
            after=min(cursors),
            tip=tip,
            known=known,
            behind=tuple(app for app, _ in behind),
        )

    def _waiting(self, owner: str) -> dict[str, list[tuple[AppConnection, int]]]:
        """The owner's sockets by session, each with the cursor it holds."""
        waiting: dict[str, list[tuple[AppConnection, int]]] = {}
        for app in self._host.apps():
            if app.username != owner:
                continue
            for session_id, cursor in app.cursors.items():
                waiting.setdefault(session_id, []).append((app, cursor))
        return waiting

    def _behind(self, item: CatchUp) -> list[AppConnection]:
        """The sockets in the gap now: those that were in it when it was planned, and any that
        subscribed from below the tip since."""
        sockets = {
            app
            for app, cursor in self._waiting(item.owner).get(item.session_id, [])
            if cursor < item.tip
        }
        sockets.update(app for app in item.behind if app.subscribed(item.session_id))
        return list(sockets)

    async def _run(self, connection: DeviceConnection, item: CatchUp, hold: Hold) -> None:
        started = time.monotonic()
        outcome, events = OUTCOME_FETCH_FAILED, 0
        try:
            outcome, events = await self._catch_up(connection, item, hold)
        except asyncio.CancelledError:
            # The link dropped, or the gateway is stopping: what is held still goes out.
            await self._give_up(item, hold)
            self._log(connection, item, started, OUTCOME_FETCH_FAILED, 0)
            raise
        except Exception:
            log.exception("catch-up failed", device_id=connection.device_id)
            self._abort(item, hold)
        self._log(connection, item, started, outcome, events)

    async def _give_up(self, item: CatchUp, hold: Hold) -> None:
        """End a fetch that did not finish: mark the gap, then let the session's live events go."""
        try:
            self._leave_behind(item, hold)
            await self.release(item.session_id, hold)
        except Exception:
            log.exception("held events could not be published", session_id=item.session_id)
            self._abort(item, hold)

    def _log(
        self, connection: DeviceConnection, item: CatchUp, started: float, outcome: str, events: int
    ) -> None:
        log.info(
            "session caught up",
            device_id=connection.device_id,
            session_id=item.session_id,
            since=item.after,
            last_seq=item.tip,
            events=events,
            ms=int((time.monotonic() - started) * 1000),
            outcome=outcome,
        )

    async def _catch_up(
        self, connection: DeviceConnection, item: CatchUp, hold: Hold
    ) -> tuple[str, int]:
        host = self._host
        deadline = time.monotonic() + host.budget()
        applied: int | None = None
        try:
            async with self._gate.slot(connection.device_id, item.session_id, within=host.budget()):
                events = await fetch_missing(
                    host.ask,
                    connection,
                    item.session_id,
                    item.after,
                    host.timeout(),
                    pages=CATCH_UP_PAGES,
                    budget=max(deadline - time.monotonic(), 0.0),
                )
                if events is not None and await self._apply(connection, item, hold, events):
                    applied = len(events)
                else:
                    self._leave_behind(item, hold)
                await self.release(item.session_id, hold)
        except TimeoutError:
            # The turn never came: nothing was fetched.
            self._leave_behind(item, hold)
            await self.release(item.session_id, hold)
        if applied is None:
            return OUTCOME_FETCH_FAILED, 0
        return OUTCOME_BACKFILLED, applied

    async def _apply(
        self, connection: DeviceConnection, item: CatchUp, hold: Hold, events: list[Frame]
    ) -> bool:
        """Publish what the device sent with what was held, in one pass, once each.

        False when none of it went out, or when the hold was let go of on the way, which leaves a
        gap the caller marks. The sockets in the gap are marked before the first event goes out and
        cleared after the last, so a pass that ends any other way, a cancel or an error included,
        leaves them marked, and every subscriber of the session with them (`_cut_short`). A socket
        that would be given more than `CATCH_UP_EVENTS` frames, with what its queue holds and what
        other passes have promised it, is sent none of the fetch and stays marked: a burst that
        overruns an app's queue costs it its socket, and half a gap is how a hole gets into a
        transcript.
        """
        host = self._host
        if not await self._may_publish(connection, item.session_id, len(events)):
            return False
        if not self._intact(item, hold):
            return False
        held = hold.take()
        batch = _merged(item.session_id, events, held)
        behind = self._behind(item)
        for app in behind:
            app.mark_stale(item.session_id)
        reserved, over = _reserve(behind, item.session_id, batch)
        done = 0
        try:
            for event, frame, fetched in batch:
                if not self._intact(item, hold):
                    return False
                await host.publish(
                    connection,
                    item.session_id,
                    event,
                    frame,
                    known=item.known if fetched else None,
                    skip=over if fetched else frozenset(),
                )
                done += 1
        finally:
            for app, frames in reserved.items():
                app.reserved -= frames
            if done < len(batch):
                self._cut_short(item, hold, held, batch, done)
        if not self._intact(item, hold):
            return False
        # The device has nothing more of the kinds history carries, so those that waited at an
        # older cursor are as current as the device's own count says, and owe no resync.
        for app in behind:
            app.advance(item.session_id, item.tip)
            if app not in over:
                app.clear_stale(item.session_id, after=item.after)
        return True

    def _cut_short(
        self, item: CatchUp, hold: Hold, held: list[tuple[Frame, Frame]], batch: Batch, done: int
    ) -> None:
        """A pass ended before its last event, and nobody can say who missed which.

        Every subscriber is marked and the replay buffer dropped (`_mark_all`). What was held and
        has not gone out, copies that a fetched one took the place of included, follows as live
        events with the session's own, into a buffer that begins with them and so has no hole.
        Where the hold was let go of, by an overflow or a replaced connection, newer events are
        out or the events are dropped for good, and publishing these now would put them last.
        """
        self._mark_all(item)
        if self._intact(item, hold):
            last = _seq(batch[done - 1][0]) if done else 0
            hold.events.extend(entry for entry in held if _seq(entry[0]) > last)

    def _leave_behind(self, item: CatchUp, hold: Hold) -> None:
        """Note that the device could not fill a gap, unless the hold was let go of already.

        Whoever is in the gap is told to resync the next time it subscribes, and a buffer with a
        hole in it is dropped, so that a later subscribe asks the device rather than trusting a
        replay that skips events. Called before the held events go out, while the cursors still
        say who is behind.
        """
        if self._holds.get(item.session_id) is not hold:
            return
        for app in self._behind(item):
            app.mark_stale(item.session_id)
        buffer = self._host.buffers.get(item.session_id)
        if buffer is not None and len(buffer) and buffer.last_seq < item.tip:
            del self._host.buffers[item.session_id]

    def _abort(self, item: CatchUp, hold: Hold) -> None:
        """A fetch ended in an error or was cancelled, and what it held may be lost."""
        if self._holds.get(item.session_id) is not hold:
            return
        del self._holds[item.session_id]
        self._mark_all(item)

    def _mark_all(self, item: CatchUp) -> None:
        """Tell every socket subscribed to the session to resync, and drop its replay buffer.

        For a pass or a fetch that ended where nobody can say who missed what: an event it was
        owed may be among those lost, a socket level with the device's hello is in no gap that
        another mark covers, and an event stored in the buffer before its fan-out was cut would be
        refused as a duplicate when it is published again.
        """
        for app, _ in self._waiting(item.owner).get(item.session_id, []):
            app.mark_stale(item.session_id)
        self._host.buffers.pop(item.session_id, None)

    # ---- the live events of a session being fetched ----

    def held(self, session_id: str) -> Hold | None:
        """The hold a live event of this session has to join, if its fetch is in flight."""
        return self._holds.get(session_id)

    async def overflow(self, session_id: str, hold: Hold) -> None:
        """The hold is full: whoever is behind is told to resync, and what is held goes out now.

        A fetch that is being published meanwhile stops there, and its mark stays.
        """
        hold.overflowed = True
        self._leave_behind(hold.item, hold)
        await self.release(session_id, hold)

    async def release(self, session_id: str, hold: Hold) -> None:
        """Publish what arrived while the fetch was in flight, in order, and let the session go."""
        while self._holds.get(session_id) is hold and hold.events:
            batch = hold.take()
            if not await self._may_publish(hold.device, session_id, len(batch)):
                continue
            for event, frame in batch:
                await self._host.publish(hold.device, session_id, event, frame)
        if self._holds.get(session_id) is hold:
            del self._holds[session_id]

    def forget(self, session_id: str) -> None:
        """The session is gone: nothing it held is published, and its fetch applies nothing."""
        self._holds.pop(session_id, None)

    def cancel(self, connection: DeviceConnection) -> None:
        """The link dropped: end the fetches that were running on it."""
        for hold in list(self._holds.values()):
            if hold.device is connection and hold.task is not None:
                hold.task.cancel()

    def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._holds.clear()

    # ---- a subscribe the buffer cannot serve ----

    async def fetch_for_subscriber(
        self, device: DeviceConnection, session_id: str, since_seq: int
    ) -> tuple[list[Frame] | None, bool]:
        """The events after ``since_seq`` from the device, and whether it was asked at all.

        ``None`` stands for events that do not fit the bounds, or a turn that did not come.
        """
        host = self._host
        deadline = time.monotonic() + host.budget()
        try:
            async with self._gate.slot(device.device_id, session_id, within=host.budget()):
                buffer = host.buffers.get(session_id)
                if buffer is not None and not buffer.replay_from(since_seq)[1]:
                    # A fetch ahead of this one has filled the buffer.
                    return [], False
                events = await fetch_missing(
                    host.ask,
                    device,
                    session_id,
                    since_seq,
                    host.timeout(),
                    budget=max(deadline - time.monotonic(), 0.0),
                )
                return events, True
        except TimeoutError:
            return None, False

    def join(self, session_id: str, fetched: list[Frame], since_seq: int) -> list[Frame]:
        """What the device sent, then what arrived live while it was being fetched."""
        newest = (int_field(fetched[-1], "seq") if fetched else since_seq) or since_seq
        buffer = self._host.buffers.get(session_id)
        return [*fetched, *(buffer.after(newest) if buffer is not None else [])]


Batch = list[tuple[Frame, Frame, bool]]


def _reserve(
    behind: list[AppConnection], session_id: str, batch: Batch
) -> tuple[dict[AppConnection, int], frozenset[AppConnection]]:
    """Promise each socket in the gap the frames a pass will queue for it, if it can take them.

    The first is what was promised, which the pass gives back when it ends. The second is the
    sockets that cannot take their share, and are sent none of the fetch.
    """
    owed = {app: _frames_for(app, session_id, batch) for app in behind}
    over = frozenset(app for app, frames in owed.items() if frames and frames > _room(app))
    reserved = {app: frames for app, frames in owed.items() if app not in over}
    for app, frames in reserved.items():
        app.reserved += frames
    return reserved, over


def _frames_for(app: AppConnection, session_id: str, batch: Batch) -> int:
    """How many frames a pass sends a socket: every held event, the fetched ones past its cursor."""
    cursor = app.cursors.get(session_id, 0)
    return sum(1 for event, _, fetched in batch if not fetched or _seq(event) > cursor)


def _seq(event: Frame) -> int:
    return int_field(event, "seq") or 0


def _room(app: AppConnection) -> int:
    """How many more frames the catch-ups may give a socket, less what it holds and is promised."""
    return CATCH_UP_EVENTS - app.queue.qsize() - app.reserved


def _merged(session_id: str, fetched: list[Frame], held: list[tuple[Frame, Frame]]) -> Batch:
    """What the device sent and what arrived live meanwhile, in one ascending pass, once each.

    A fetched event outranks a held one of the same `seq`. A held event that history does not
    carry (a delta, a status, a queue) keeps its place between the fetched ones, which is where an
    app that drops whatever is at or below its cursor needs it.
    """
    merged: dict[int, tuple[Frame, Frame, bool]] = {}
    for event, frame in held:
        seq = int_field(event, "seq")
        if seq is not None:
            merged[seq] = (event, frame, False)
    for event in fetched:
        frame = {"type": "session.event", "session_id": session_id, "event": event}
        merged[int_field(event, "seq") or 0] = (event, frame, True)
    return [merged[seq] for seq in sorted(merged)]


def _session_tips(summaries: list[Frame]) -> dict[str, int]:
    """Where each session of a hello says it has got to, by session id."""
    tips: dict[str, int] = {}
    for summary in summaries:
        session_id = text_field(summary, "session_id")
        tip = int_field(summary, "last_seq")
        if session_id and tip is not None:
            tips[session_id] = tip
    return tips
