"""What the board's parts share: the gateway they run in, and the state they keep (A62).

The gateway is reached through three small interfaces, so the board can be driven in a test
without one: the hub (the device, the account a session belongs to, the queue, the app sockets),
the session index (how far a session has gone) and the account preferences (whether the board is
on). Everything kept in memory — the followed boards, the watchers, the caps — lives here, in one
process, as the hub's own state does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..connections import AppConnection
from ..index import IndexedSession
from ..preference_store import Preferences
from .follow import FollowedSet
from .history import Frame
from .limits import CONCURRENT_CALLS
from .pacing import HourlyCalls, Verdicts
from .policy import BoardPolicy
from .store import BoardRow, BoardStore
from .watchers import Watchers
from .wire import MODEL_FAILED, RATE_LIMITED, Status, stale


class BoardHost(Protocol):
    async def ask_device(self, device_id: str, request: Frame, timeout: float) -> Frame | None: ...

    async def session_account(self, session_id: str) -> tuple[str, str] | None: ...

    async def send_app(self, connection: AppConnection, frame: Frame) -> None: ...

    def queue_snapshot(self, session_id: str) -> Frame | None: ...

    def device_online(self, device_id: str) -> bool: ...


class SessionLookup(Protocol):
    async def get(self, session_id: str) -> IndexedSession | None: ...


class PreferenceLookup(Protocol):
    async def get(self, username: str) -> Preferences: ...


@dataclass(frozen=True, eq=False)
class RunTicket:
    """One update of one session's board, while it may still write and send."""

    session_id: str
    username: str


def queue_hash(snapshot: Frame | None) -> str:
    """A fingerprint of the queued texts, empty when nothing is queued."""
    pending = snapshot.get("pending") if isinstance(snapshot, dict) else None
    texts = [item.get("text") for item in pending if isinstance(item, dict)] if pending else []
    if not texts:
        return ""
    encoded = json.dumps(texts, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass
class Context:
    host: BoardHost
    sessions: SessionLookup
    store: BoardStore
    preferences: PreferenceLookup
    policy: BoardPolicy
    clock: Callable[[], float]
    watchers: Watchers
    followed: FollowedSet = field(default_factory=FollowedSet)
    hourly: HourlyCalls = field(default_factory=HourlyCalls)
    verdicts: Verdicts = field(default_factory=Verdicts)
    #: The reason of each session's last failure that earned a back-off, for the status it reads.
    #: Memory only: after a restart a board still backing off reads the generic `model_failed`,
    #: which the apps word as the other two such failures are.
    failures: dict[str, str] = field(default_factory=dict)
    #: Model calls this process makes at once; the others wait in turn.
    calls: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(CONCURRENT_CALLS))
    #: The update running for each session. Deleting the board, or the account's boards, takes it
    #: away, and an update whose ticket is gone writes nothing and sends nothing more.
    runs: dict[str, RunTicket] = field(default_factory=dict)

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def begin_run(self, session_id: str, username: str) -> RunTicket:
        ticket = RunTicket(session_id, username)
        self.runs[session_id] = ticket
        return ticket

    def running(self, ticket: RunTicket) -> bool:
        return self.runs.get(ticket.session_id) is ticket

    def end_run(self, ticket: RunTicket) -> None:
        if self.running(ticket):
            del self.runs[ticket.session_id]

    def stop_runs(self, session_id: str) -> None:
        self.runs.pop(session_id, None)

    def stop_user_runs(self, username: str) -> None:
        for session_id, ticket in list(self.runs.items()):
            if ticket.username == username:
                del self.runs[session_id]

    async def behind(self, session_id: str, row: BoardRow) -> bool:
        """Whether the session went on since the board last looked, or its queue changed."""
        indexed = await self.sessions.get(session_id)
        last_seq = indexed.last_seq if indexed is not None else 0
        snapshot = self.host.queue_snapshot(session_id)
        return last_seq > row.checked_seq or queue_hash(snapshot) != row.queue_hash

    def held_back(self, row: BoardRow, *, force: bool) -> Status | None:
        """The status of a board that may not be updated now: in a back-off, or over the cap."""
        now = self.now_ms()
        if not force and row.retry_at is not None and row.retry_at > now:
            return stale(self.failures.get(row.session_id, MODEL_FAILED), row.retry_at)
        retry_at = self.hourly.retry_at(row.username, now)
        if retry_at is not None:
            return stale(RATE_LIMITED, retry_at)
        return None

    async def refusal(self, username: str) -> str | None:
        """Why the policy will not update the account's boards now, cached or quick, or None."""
        cached = self.verdicts.get(username, self.now_ms())
        if cached is not None:
            return cached
        return await self.policy.quick(username)
