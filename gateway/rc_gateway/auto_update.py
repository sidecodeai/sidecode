"""Amendments A36 and A56: the gateway brings every device to the wheel it serves, when it is quiet.

Updating a client used to be a person's job — an app drew "Update available" on the row and
somebody pressed Update, on every machine, after every release. The gateway does it itself now,
and this module is the whole policy, so the hub keeps routing frames and one place decides who is
asked, when, and how often. Nothing here touches the wire: the request, its replies and
``Device.update_state`` are A22's.

The rules, from A36 and §8 rule 18, as A56 amends them:

* A ``hello`` whose ``client_build`` is neither null nor the served build earns one
  ``device.update``, sent on the gateway's own account (``from: "gateway"``), once the device has
  been quiet for two minutes. An update restarts the device's service, which a session someone is
  working in feels, so it waits for the machine to be left alone rather than interrupting it.
* Quiet means that none of the device's sessions that is driven or attached (``control``
  ``remote`` or ``shared``) has a turn in progress or a message queued. The clock starts over
  whenever that changes, and the gateway follows the device's summaries to know.
* A device that is never quiet is asked anyway after six hours, once: a client up to 1.18.1 does
  not guard a session a terminal shares, and a gateway must not leave one on an old build for ever.
* ``conflict`` means the device itself found it busy, by a rule of its own that is wider than the
  gateway's. Wait for quiet again, and ask no more than once in ten minutes. The one conflict that
  is not worth repeating is the device saying it already runs the build: its next ``hello``
  carries that build anyway.
* ``unsupported`` means the client cannot update itself, having been installed from source. It is
  left alone for as long as the connection lasts.
* A failure — ``update.failed``, or a device that never comes back — is a person's to look at. The
  build that failed is remembered on the device row (``update_failed_build``, written by the hub),
  and no automatic attempt follows until an app retries or the gateway serves a newer wheel.

A restart needs no state of its own: every connected device re-says ``hello``, and the policy runs
again from there.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .connections import DeviceConnection
from .devices import UPDATE_RUNNING, DeviceRecord
from .frames import (
    DEVICE_UPDATE,
    ERROR_CONFLICT,
    ERROR_UNSUPPORTED,
    Frame,
    object_field,
    text_field,
)
from .logging import logger

log = logger("rc_gateway.auto_update")

#: The least time between two asks of one device (A36, A56): a device that answers `conflict` is
#: asked again once it is quiet, and no sooner than this after the last time.
UPDATE_RETRY_SECONDS = 600.0
#: How long a device must have been left alone before it is asked (A56).
UPDATE_QUIET_SECONDS = 120.0
#: How long a device that is never quiet is waited for, before it is asked anyway (A56).
UPDATE_PATIENCE_SECONDS = 6 * 60 * 60.0

#: A turn in progress, as a session's `state` says it (A22). Together with `queued`, what keeps a
#: device from being updated, in a session the device drives or is attached to.
BUSY_STATES = frozenset({"starting", "running", "needs_approval", "needs_input"})
#: The sessions a person can be working in: the device drives them or is attached.
DRIVEN_CONTROLS = frozenset({"remote", "shared"})

#: The client's words for "the build you are asking for is the one I run" (A22). Every other
#: conflict is a device that is busy, and worth asking again; this one never is. Should the client
#: ever reword it, the cost is one wasted retry, not a wrong state.
ALREADY_ON_BUILD = "already on this build"

_ACCEPTED = "accepted"
_BUSY = "busy"
_UNSUPPORTED = "unsupported"
_UNANSWERED = "unanswered"


@dataclass(frozen=True)
class DeviceUpdates:
    """What the policy needs from the hub, as callables, so a test can drive it without one."""

    #: Send a gateway-originated request to this device and wait for its reply. An accepted
    #: ``device.update`` moves the device to ``updating`` on the way back through the hub, exactly
    #: as an app's does (A22), so this policy only decides who is asked and when.
    ask: Callable[[DeviceConnection, Frame], Awaitable[Frame | None]]
    #: The connection that currently holds the device's slot, if any.
    connection: Callable[[str], DeviceConnection | None]
    #: The stored device row: its build, its update state and the build that last failed on it.
    record: Callable[[str], Awaitable[DeviceRecord | None]]


@dataclass(frozen=True)
class UpdatePace:
    """How soon, how patiently and how often a device is asked (A56)."""

    #: The least time between two asks of one device.
    retry: float = UPDATE_RETRY_SECONDS
    #: How long a device must have been left alone before it is asked.
    quiet: float = UPDATE_QUIET_SECONDS
    #: How long a device that is never quiet is waited for, before it is asked anyway.
    patience: float = UPDATE_PATIENCE_SECONDS


@dataclass
class _Watch:
    """One device's automatic update, for as long as the connection that earned it lives."""

    #: The connection this belongs to. A replacement starts over, as A36's "while it stays
    #: connected" means this socket and not the machine.
    connection_id: str
    #: The sessions that keep the device from being updated: the hello's, then every summary since.
    busy: set[str] = field(default_factory=set)
    #: When the device last became quiet, or ``None`` while it is not. On the monotonic clock.
    quiet_since: float | None = None
    #: Set whenever the device turns busy or quiet, so a parked attempt starts its wait over.
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    #: The earliest the device may be asked again, once it has been asked.
    not_before: float = 0.0
    task: asyncio.Task[None] | None = None
    #: The device answered `unsupported`: it cannot update itself, so it is never asked again.
    unsupported: bool = False

    def note(self, session_id: str, busy: bool) -> None:
        """One session of the device is busy now, or is not."""
        was_busy = bool(self.busy)
        if busy:
            self.busy.add(session_id)
        else:
            self.busy.discard(session_id)
        if bool(self.busy) != was_busy:
            self.quiet_since = None if self.busy else time.monotonic()
            self.changed.set()

    def replace(self, busy: set[str]) -> None:
        """What a hello says is busy, in place of what was known."""
        for session_id in self.busy - busy:
            self.note(session_id, False)
        for session_id in busy:
            self.note(session_id, True)


class AutoUpdate:
    """Asks devices to install the served wheel, one attempt per device at a time (A36, A56)."""

    def __init__(
        self,
        updates: DeviceUpdates,
        *,
        served_build: Callable[[], str | None],
        pace: UpdatePace = UpdatePace(),
    ) -> None:
        self._updates = updates
        self._build = served_build
        self._pace = pace
        self._watches: dict[str, _Watch] = {}

    def served_build(self) -> str | None:
        """The build the gateway serves, or None in a checkout that has never built a wheel."""
        return self._build()

    def on_hello(
        self, connection: DeviceConnection, client_build: str | None, summaries: list[Frame]
    ) -> None:
        """A device announced itself: bring it to the served build unless it is already there.

        This runs on the device's read loop, so it only spawns the attempt: asking waits for the
        device's reply, and waiting here would hold the very loop that has to deliver it.
        ``summaries`` are the sessions the hello listed, from which the device's quiet is known.
        """
        device_id = connection.device_id
        watch = self._watches.get(device_id)
        if watch is not None and watch.connection_id != connection.id:
            self._cancel(device_id)
            watch = None
        build = self._build()
        if build is None or not client_build or client_build == build:
            return
        if watch is not None and (watch.unsupported or _running(watch)):
            return
        busy = {text_field(item, "session_id") for item in summaries if is_busy(item)}
        if watch is None:
            watch = _Watch(connection_id=connection.id, quiet_since=time.monotonic())
            self._watches[device_id] = watch
        watch.replace(busy)
        # A hello after an attempt that ended is a new attempt, not a repeat of that one.
        watch.not_before = 0.0
        watch.task = asyncio.create_task(self._pursue(connection))

    def on_session_change(self, device_id: str, summary: Frame) -> None:
        """A session of this device changed: it may have left the device busy, or quiet."""
        watch = self._watches.get(device_id)
        if watch is not None:
            watch.note(text_field(summary, "session_id"), is_busy(summary))

    def on_session_removed(self, device_id: str, session_id: str) -> None:
        watch = self._watches.get(device_id)
        if watch is not None:
            watch.note(session_id, False)

    def forget(self, connection: DeviceConnection) -> None:
        """This connection is gone: drop what it earned, so the next one starts over.

        Scoped to the connection because a device that reconnects has already been given a new
        attempt by its new ``hello``, and that one is not this one's to cancel.
        """
        watch = self._watches.get(connection.device_id)
        if watch is not None and watch.connection_id == connection.id:
            self._cancel(connection.device_id)

    def stop(self) -> None:
        for device_id in list(self._watches):
            self._cancel(device_id)

    def _cancel(self, device_id: str) -> None:
        watch = self._watches.pop(device_id, None)
        if watch is not None and watch.task is not None:
            watch.task.cancel()

    async def _pursue(self, connection: DeviceConnection) -> None:
        """Ask this device for the served build once it is quiet, and again while it is busy.

        The build is read again before every attempt rather than carried from the hello: a wheel
        deployed while this one was parked is the one the device should be brought to.
        """
        device_id = connection.device_id
        while True:
            watch = self._watches.get(device_id)
            if watch is None or watch.connection_id != connection.id:
                return
            await self._wait_for_quiet(watch)
            build = self._build()
            if build is None or not await self._worth_asking(connection, build):
                return
            if self._watches.get(device_id) is not watch:
                return
            if not await self._ask(connection, watch, build):
                return

    async def _ask(self, connection: DeviceConnection, watch: _Watch, build: str) -> bool:
        """Ask once. True when the device was busy, so that it is worth asking again."""
        reply = await self._updates.ask(connection, {"type": DEVICE_UPDATE, "build": build})
        watch.not_before = time.monotonic() + self._pace.retry
        outcome = _outcome(reply)
        log.info(
            "automatic client update",
            device_id=connection.device_id,
            build=build[:12],
            outcome=outcome,
        )
        if outcome == _UNSUPPORTED:
            watch.unsupported = True
        return outcome == _BUSY

    async def _wait_for_quiet(self, watch: _Watch) -> None:
        """Park until the device has been quiet long enough, or patience runs out (A56).

        Never before ``not_before``: a device that answered `conflict` is not asked again within
        the retry period, however quiet it looks to the gateway.
        """
        give_up_at = time.monotonic() + self._pace.patience
        while True:
            watch.changed.clear()
            ready_at = give_up_at
            if watch.quiet_since is not None:
                ready_at = min(ready_at, watch.quiet_since + self._pace.quiet)
            wait = max(ready_at, watch.not_before) - time.monotonic()
            if wait <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(watch.changed.wait(), timeout=wait)

    async def _worth_asking(self, connection: DeviceConnection, build: str) -> bool:
        """Whether this device is still the one to ask, and still behind the served build."""
        device_id = connection.device_id
        if self._updates.connection(device_id) is not connection:
            return False
        record = await self._updates.record(device_id)
        if record is None or record.client_build is None or record.client_build == build:
            return False
        if record.update_state == UPDATE_RUNNING:
            return False
        if record.update_failed_build == build:
            # This wheel already failed on this machine. Hammering it would only rewrite the same
            # failure; a person's Retry, or a newer wheel, is what moves it on (A36).
            log.info("automatic client update skipped after a failure", device_id=device_id)
            return False
        return True


def is_busy(summary: Frame) -> bool:
    """Whether a session keeps its device from being updated (A56).

    Only a session the device drives or is attached to counts: a turn in progress there, or a
    message queued for it. A queue left on a session nothing holds must not stop updates for ever.
    """
    if text_field(summary, "control") not in DRIVEN_CONTROLS:
        return False
    if text_field(summary, "state") in BUSY_STATES:
        return True
    queued = summary.get("queued")
    return isinstance(queued, int) and not isinstance(queued, bool) and queued > 0


def _running(watch: _Watch) -> bool:
    return watch.task is not None and not watch.task.done()


def _outcome(reply: Frame | None) -> str:
    """Name what the device answered, in the terms A36's rules are written in."""
    if reply is None:
        return _UNANSWERED
    if reply.get("ok") is True:
        return _ACCEPTED
    error = object_field(reply, "error") or {}
    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    if code == ERROR_UNSUPPORTED:
        return _UNSUPPORTED
    if code == ERROR_CONFLICT and message != ALREADY_ON_BUILD:
        return _BUSY
    return code or "refused"
