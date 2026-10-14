"""Amendment A36: the gateway brings every device to the wheel it serves.

Updating a client used to be a person's job — an app drew "Update available" on the row and
somebody pressed Update, on every machine, after every release. The gateway does it itself now,
and this module is the whole policy, so the hub keeps routing frames and one place decides who is
asked, when, and how often. Nothing here touches the wire: the request, its replies and
``Device.update_state`` are A22's.

The rules, from A36 and §8 rule 18:

* A ``hello`` whose ``client_build`` is neither null nor the served build earns one
  ``device.update``, sent on the gateway's own account (``from: "gateway"``).
* ``conflict`` means the device is busy. Ask again when its sessions go quiet, or in ten minutes,
  whichever comes first, for as long as it stays connected and behind. The one conflict that is
  not worth repeating is the device saying it already runs the build: its next ``hello`` carries
  that build anyway.
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .connections import DeviceConnection
from .devices import UPDATE_RUNNING, DeviceRecord
from .frames import DEVICE_UPDATE, ERROR_CONFLICT, ERROR_UNSUPPORTED, Frame, object_field
from .logging import logger

log = logger("rc_gateway.auto_update")

#: How long a busy device is left alone before it is asked again (A36). The quiet moment usually
#: comes first; this is the floor under a device whose sessions report nothing for a long while.
UPDATE_RETRY_SECONDS = 600.0

#: What the device itself counts as busy when it refuses an update (A22, the client's `daemon.py`).
#: The gateway asks again once no session of the device is in one of these states.
BUSY_STATES = frozenset({"starting", "running", "needs_approval", "needs_input"})

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
    #: Whether any session on the device is mid-turn.
    busy: Callable[[str], Awaitable[bool]]


@dataclass
class _Watch:
    """One device's automatic update, for as long as the connection that earned it lives."""

    #: The connection this belongs to. A replacement starts over, as A36's "while it stays
    #: connected" means this socket and not the machine.
    connection_id: str
    quiet: asyncio.Event
    task: asyncio.Task[None] | None = None
    #: True while an attempt is parked waiting for the device's sessions to finish.
    waiting: bool = False
    #: The device answered `unsupported`: it cannot update itself, so it is never asked again.
    unsupported: bool = False


class AutoUpdate:
    """Asks devices to install the served wheel, one attempt per device at a time (A36)."""

    def __init__(
        self,
        updates: DeviceUpdates,
        *,
        served_build: Callable[[], str | None],
        retry_after: float = UPDATE_RETRY_SECONDS,
    ) -> None:
        self._updates = updates
        self._build = served_build
        self._retry_after = retry_after
        self._watches: dict[str, _Watch] = {}

    def served_build(self) -> str | None:
        """The build the gateway serves, or None in a checkout that has never built a wheel."""
        return self._build()

    def on_hello(self, connection: DeviceConnection, client_build: str | None) -> None:
        """A device announced itself: bring it to the served build unless it is already there.

        This runs on the device's read loop, so it only spawns the attempt: asking waits for the
        device's reply, and waiting here would hold the very loop that has to deliver it.
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
        if watch is None:
            watch = _Watch(connection_id=connection.id, quiet=asyncio.Event())
            self._watches[device_id] = watch
        watch.task = asyncio.create_task(self._pursue(connection))

    async def on_session_change(self, device_id: str) -> None:
        """A session of this device changed: release a parked attempt once nothing is running."""
        watch = self._watches.get(device_id)
        if watch is None or not watch.waiting:
            return
        if await self._updates.busy(device_id):
            return
        watch.quiet.set()

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
        """Ask this device for the served build, and keep asking while it is merely busy.

        The build is read again before every attempt rather than carried from the hello: a wheel
        deployed while this one was parked is the one the device should be brought to.
        """
        device_id = connection.device_id
        while True:
            build = self._build()
            if build is None or not await self._worth_asking(connection, build):
                return
            watch = self._watches.get(device_id)
            if watch is None or watch.connection_id != connection.id:
                return
            reply = await self._updates.ask(connection, {"type": DEVICE_UPDATE, "build": build})
            outcome = _outcome(reply)
            log.info(
                "automatic client update",
                device_id=device_id,
                build=build[:12],
                outcome=outcome,
            )
            if outcome == _ACCEPTED:
                return
            if outcome == _UNSUPPORTED:
                watch.unsupported = True
                return
            if outcome != _BUSY:
                return
            await self._wait_for_quiet(watch)

    async def _wait_for_quiet(self, watch: _Watch) -> None:
        """Park until the device's sessions finish, or until the retry period is up."""
        watch.quiet.clear()
        watch.waiting = True
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(watch.quiet.wait(), timeout=self._retry_after)
        finally:
            watch.waiting = False

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
