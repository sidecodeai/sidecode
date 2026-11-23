"""What the presence rules mean for the device's Antigravity sessions (A64, 4.4, A58).

Every ten seconds, and again just before the device opens a conversation, the `agy` processes it
did not start are found (`presence`). A conversation one of them may have open becomes `terminal`
and `readonly`, and a message to it is refused; when it is let go it goes back to `remote` if the
device holds it, `none` otherwise. The device's own idle process for such a conversation is stopped
first, since two writers lose a message, whatever `agy` still had running in it. One in the middle
of a turn of the device's own cannot stop: the device says so once, keeps the session `remote`, and
gives it up when the turn ends, before anything queued for it goes out. Queued messages wait for
the device to have the conversation again, still editable and removable (A43).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...logging_setup import logger
from . import errors, presence
from .exemption import exempt
from .presence import Holders, Place

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry, SessionHub

log = logger("rc_client.antigravity.holding")

AGENT = "antigravity"

Places = Callable[["SessionEntry"], Place]


def turn_running(entry: SessionEntry) -> bool:
    """Whether a turn of the device's own runs in its process for the session: rule 1 of 4.4."""
    runner = entry.runner
    return runner is not None and runner.busy


class Holding:
    """Applies the presence rules to the hub's Antigravity sessions."""

    def __init__(self, hub: SessionHub | None, places: Places) -> None:
        self.hub = hub
        self._places = places
        # The sessions told once that an `agy` may have them open while a turn of the device's ran.
        self._warned: set[str] = set()

    async def scan(self, binary: str | None) -> None:
        """The ten-second look at every Antigravity session the device knows."""
        hub = self.hub
        if hub is None:
            return
        entries = [entry for entry in hub.entries.values() if entry.session.agent == AGENT]
        if not entries:
            return
        holders = await presence.scan(binary, cwds=self._wants_cwds(entries))
        for entry in entries:
            if hub.entries.get(entry.session.session_id) is not entry:
                continue
            verdict = self._verdict(entry, holders)
            if verdict is not None:
                await self.apply(entry, verdict)

    async def check(self, entry: SessionEntry, binary: str | None) -> None:
        """Just before a conversation opens: refused, and shown held, when a holder may have it."""
        holders = await presence.scan(binary, cwds=self._wants_cwds([entry]))
        verdict = self._verdict(entry, holders)
        if verdict is None:
            raise errors.scan_failed()
        if verdict:
            if self.hub is not None and entry.runner is None:
                await self.hub.terminal_holds(entry, True)
            raise errors.held()

    async def settle(self, entry: SessionEntry, binary: str | None) -> None:
        """A turn of the device's ended: a session it warned about is let go of now, if it must.

        So is one a queued message is about to go to: a holder that came since the last scan
        takes it before that message leaves the queue.
        """
        if entry.session.session_id not in self._warned and not entry.queue:
            return
        holders = await presence.scan(binary, cwds=self._wants_cwds([entry]))
        verdict = self._verdict(entry, holders)
        if verdict is not None:
            await self.apply(entry, verdict)

    def _wants_cwds(self, entries: list[SessionEntry]) -> bool:
        return any(not exempt(entry) and not self._places(entry).no_directory for entry in entries)

    def _verdict(self, entry: SessionEntry, holders: Holders) -> bool | None:
        session = entry.session
        return presence.holds(
            session.session_id, self._places(entry), holders, exempt=exempt(entry)
        )

    async def apply(self, entry: SessionEntry, held: bool) -> None:
        hub = self.hub
        if hub is None or entry.ending:
            return
        session_id = entry.session.session_id
        if not held:
            self._warned.discard(session_id)
            if entry.runner is None and entry.session.control == "terminal":
                await hub.terminal_holds(entry, False)
            return
        if entry.runner is not None:
            if turn_running(entry):
                if session_id not in self._warned:
                    self._warned.add(session_id)
                    await entry.channel.notice("warn", errors.ALSO_OPEN)
                return
            if not await hub.release_process(entry, keep_queue=True):
                return
            log.info("let an agy the device did not start have a conversation, stopping its own")
        self._warned.discard(session_id)
        if entry.session.control != "terminal":
            await hub.terminal_holds(entry, True)
