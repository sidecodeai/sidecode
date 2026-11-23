"""What the device runs for Antigravity beside its sessions (A64).

Once Antigravity has started on the device, its plugin's service lists `agy`'s conversations every
30 seconds while the gateway link is up, the first as soon as the link is, looks for `agy`
processes the device did not start every ten seconds, and keeps the readings of the account's
quota. It is the one place a session's runner asks before it opens a conversation, and tells when a
turn ends.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ...config import MirrorConfig
from ...errors import RcError
from ...logging_setup import logger
from . import catalog, runtime
from .conversations import Conversations
from .holding import Holding
from .listing import Listed, Lister
from .options import Running
from .presence import Place
from .usage import Usage

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry, SessionHub

log = logger("rc_client.antigravity.service")

LIST_EVERY = 30.0
SCAN_EVERY = 10.0
# How soon a list the gateway link was down for is tried again, so the first follows the link.
LINK_WAIT = 1.0

Refresh = Callable[[], Awaitable[None]]


class AntigravityService:
    """The list process, the presence scan and the quota readings of one device."""

    def __init__(self) -> None:
        self.hub: SessionHub | None = None
        self._refresh: Refresh | None = None
        self.lister = Lister()
        self.usage = Usage()
        self._conversations: Conversations | None = None
        self.holding = Holding(None, self.place)
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def signed_out(self) -> bool:
        """Whether `agy` answered the device's last list with `Authentication required`."""
        return self.lister.signed_out is True

    async def start(self, hub: SessionHub, refresh: Refresh, limits: MirrorConfig) -> None:
        self.hub = hub
        self._refresh = refresh
        self._conversations = Conversations(hub, limits)
        self.holding = Holding(hub, self.place)
        self._tasks = [
            asyncio.create_task(self._every(LIST_EVERY, self.list_once), name="agy-list"),
            asyncio.create_task(self._every(SCAN_EVERY, self.scan_once), name="agy-presence"),
        ]

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.lister.close()

    async def _every(self, seconds: float, step: Callable[[], Awaitable[bool | None]]) -> None:
        """Run `step` every `seconds`; one that says False, as it waits for the link, sooner."""
        while True:
            ran: bool | None = None
            try:
                ran = await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("an Antigravity check failed")
            await asyncio.sleep(LINK_WAIT if ran is False else seconds)

    async def list_once(self) -> bool:
        """Read `agy`'s list and publish what is new; False when the gateway link is down."""
        hub, conversations = self.hub, self._conversations
        binary = runtime.resolve_binary()
        if hub is None or conversations is None or binary is None:
            return True
        if not hub.link_up():
            return False
        try:
            listed = await self.lister.fetch(binary)
        except RcError as exc:
            log.info("agy's list could not be read", reason=exc.code)
            return True
        await conversations.apply(listed)
        return True

    async def scan_once(self) -> None:
        await self.holding.scan(runtime.resolve_binary())

    # ------------------------------------------------------- for the runners

    def place(self, entry: SessionEntry) -> Place:
        """Where `agy`'s list puts a conversation; one it does not list is where it was made."""
        listed = self.lister.listed.get(entry.session.session_id)
        if listed is None:
            return Place(entry.session.cwd)
        return Place(listed.cwd, listed.no_directory)

    async def fresh_list(self, binary: str) -> dict[str, Listed]:
        return await self.lister.fetch(binary)

    async def check_presence(self, entry: SessionEntry, binary: str) -> None:
        await self.holding.check(entry, binary)

    async def turn_ended(self, entry: SessionEntry) -> None:
        await self.holding.settle(entry, runtime.resolve_binary())

    async def options_seen(self, started: Running) -> None:
        """A session just opened: what `agy` offered it is what detection reports from now on."""
        if catalog.remember(started) and self._refresh is not None:
            await self._refresh()
