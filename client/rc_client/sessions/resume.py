"""Resuming a session the vendor's usage limit stopped (amendment A35, 7.2).

The device ends such a turn with `stop_reason: "error"` and a `LimitStop`
(`limits.py`), and when the account's `resume_after_limit` is on this schedules
one resume a minute after the window resets, keeps it in the registry so a
restart does not lose it, and sends one fixed sentence into the session when the
time comes. Every step is a `resume` event and a new `Session.resume`.

The switch is the account's and lives on the gateway, which sends it as the
`preferences` frame after `hello_ack` and on every change; the last value is
kept in the registry so a device that is offline when a limit is reached still
knows what the person asked for. Absent means off.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import RcError
from ..logging_setup import logger
from ..models import Session, SessionResume, now_ms
from .limits import FIVE_HOURS, LimitStop

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionHub

log = logger("rc_client.resume")

# The prompt, verbatim from PROTOCOL 7.2. The same on every device, never
# edited per session: the agent knows what it was doing, and a longer
# instruction would steer it.
RESUME_PROMPT = (
    "The usage limit has reset. Continue where you left off, "
    "and let any subagents you started continue their work."
)

# Where the preference is kept between connections.
PREFERENCE_KEY = "preferences.resume_after_limit"
# How long after the reset the prompt goes out, and how often pending resumes
# are looked at: a machine that slept through the time fires on waking.
LEAD_MS = 60 * 1000
CHECK_INTERVAL = 30.0
# A person may move a resume no closer than a minute and no further than eight
# days (PROTOCOL 6.3).
MIN_AHEAD_MS = 60 * 1000
MAX_AHEAD_MS = 8 * 24 * 60 * 60 * 1000
# How many resumed turns may run into the limit again before the device stops.
MAX_ATTEMPTS = 3
# `at` of a resume that has been sent: the record stays only to count the
# attempts, and nothing is pending on the session.
FIRED = 0
# Controls a limit stop schedules a resume for. A `terminal` session gets none:
# the device has no way in.
RESUMABLE_CONTROL = frozenset({"remote", "shared"})
# States in which a turn is under way, so somebody took the session further.
BUSY_STATES = frozenset({"starting", "running", "needs_approval", "needs_input"})

TERMINAL_CLOSED = "The terminal that owned this session was closed."
PERSON_SENT = "you sent a message"
ALREADY_RUNNING = "the session is running again"
SWITCHED_OFF = "resuming after the limit reset was switched off"
OUT_OF_ATTEMPTS = "the usage limit was reached three times"


@dataclass(slots=True)
class PendingResume:
    """One stored resume: what the wire carries, plus who held the session.

    `control` is the session's control when the resume was scheduled, which is
    the only way to tell a terminal that has since been closed from a session
    the device runs itself.
    """

    session_id: str
    at: int
    estimated: bool = False
    attempts: int = 0
    window_minutes: int | None = None
    control: str = "remote"

    @property
    def pending(self) -> bool:
        return self.at > FIRED

    def to_wire(self) -> SessionResume:
        return SessionResume(
            at=self.at,
            estimated=self.estimated,
            attempts=self.attempts,
            window_minutes=self.window_minutes,
        )


class ResumeScheduler:
    """The device's side of PROTOCOL 7.2: one loop, one record per session."""

    def __init__(self, hub: SessionHub, clock: Callable[[], int] = now_ms) -> None:
        self.hub = hub
        self.registry = hub.registry
        self._clock = clock
        self._records: dict[str, PendingResume] = {}
        self._enabled = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def enabled(self) -> bool:
        return self._enabled

    def load(self) -> None:
        """Read the switch and every stored resume back, after `SessionHub.load`."""
        self._enabled = self.registry.get_kv(PREFERENCE_KEY) == "1"
        for row in self.registry.load_resumes():
            record = PendingResume(**row)
            entry = self.hub.entries.get(record.session_id)
            if entry is None:
                # The session is gone; so is anything that was pending for it.
                self.registry.delete_resume(record.session_id)
                continue
            self._records[record.session_id] = record
            if record.pending:
                entry.session.resume = record.to_wire()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="resume-scheduler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the resume scheduler failed a pass")
            await asyncio.sleep(CHECK_INTERVAL)

    # ------------------------------------------------------------ preference

    async def set_enabled(self, enabled: bool) -> None:
        """Apply the account's switch. Turning it off cancels every resume."""
        self.registry.set_kv(PREFERENCE_KEY, "1" if enabled else "0")
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if enabled:
            # Turning it on schedules nothing for stops that already happened.
            return
        for record in list(self._records.values()):
            await self._forget(record, "cancelled" if record.pending else None, SWITCHED_OFF)

    # ----------------------------------------------------------- turn ending

    async def on_turn_end(self, session: Session, limit: LimitStop | None) -> None:
        """Every turn reports here; only one that hit the limit schedules a resume."""
        if limit is None:
            record = self._records.get(session.session_id)
            if record is not None and not record.pending:
                # The session got past the limit, so the attempts start again.
                await self._forget(record, None, "")
            return
        await self._schedule_after_limit(session, limit)

    async def _schedule_after_limit(self, session: Session, limit: LimitStop) -> None:
        if not self._enabled or session.control not in RESUMABLE_CONTROL:
            return
        entry = self.hub.entries.get(session.session_id)
        if entry is None:
            return
        previous = self._records.get(session.session_id)
        attempts = previous.attempts + 1 if previous is not None else 0
        if attempts >= MAX_ATTEMPTS:
            assert previous is not None
            await self._forget(previous, "dropped", OUT_OF_ATTEMPTS)
            return
        record = PendingResume(
            session_id=session.session_id,
            at=self._at_for(limit),
            estimated=limit.resets_at_ms is None,
            attempts=attempts,
            window_minutes=limit.window_minutes,
            control=session.control,
        )
        status = "rescheduled" if previous is not None else "scheduled"
        await self._remember(record, status)

    def _at_for(self, limit: LimitStop) -> int:
        """A minute after the reset, or the window's length from now (7.2)."""
        if limit.resets_at_ms is not None:
            return limit.resets_at_ms + LEAD_MS
        minutes = limit.window_minutes or FIVE_HOURS
        return self._clock() + minutes * 60 * 1000

    # -------------------------------------------------------------- requests

    async def set_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """`session.resume_set`: schedule a resume, or move the pending one."""
        entry = self.hub.entry(str(params.get("session_id") or ""))
        at = params.get("at")
        if isinstance(at, bool) or not isinstance(at, int | float):
            raise RcError("bad_request", "at must be a timestamp in milliseconds")
        at = int(at)
        now = self._clock()
        if at < now + MIN_AHEAD_MS:
            raise RcError("bad_request", "a resume must be at least a minute ahead")
        if at > now + MAX_AHEAD_MS:
            raise RcError("bad_request", "a resume must be within eight days")
        session = entry.session
        if session.state in BUSY_STATES:
            raise RcError("conflict", "wait for the turn to finish")
        if session.control == "terminal":
            raise RcError("conflict", "controlled by the terminal on this device")
        previous = self._records.get(session.session_id)
        record = PendingResume(
            session_id=session.session_id,
            at=at,
            estimated=False,
            attempts=previous.attempts if previous is not None else 0,
            window_minutes=previous.window_minutes if previous is not None else None,
            control=session.control,
        )
        status = "rescheduled" if previous is not None and previous.pending else "scheduled"
        await self._remember(record, status)
        return {"session": session.to_dict()}

    async def cancel_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """`session.resume_cancel`: remove the pending resume; idempotent."""
        entry = self.hub.entry(str(params.get("session_id") or ""))
        record = self._records.get(entry.session.session_id)
        if record is not None:
            await self._forget(record, "cancelled" if record.pending else None, "")
        return {"session": entry.session.to_dict()}

    async def cancelled_by_person(self, session_id: str) -> None:
        """A message or a command the person sent: they got there first (6.3)."""
        record = self._records.get(session_id)
        if record is None or not record.pending:
            return
        await self._forget(record, "cancelled", PERSON_SENT)

    # --------------------------------------------------------------- firing

    async def tick(self) -> None:
        """Send every resume whose time has passed, one pass, in time order."""
        async with self._lock:
            now = self._clock()
            due = [
                record
                for record in sorted(self._records.values(), key=lambda item: item.at)
                if record.pending and record.at <= now
            ]
            for record in due:
                await self._fire(record)

    async def _fire(self, record: PendingResume) -> None:
        entry = self.hub.entries.get(record.session_id)
        if entry is None:
            self._records.pop(record.session_id, None)
            self.registry.delete_resume(record.session_id)
            return
        session = entry.session
        if session.state in BUSY_STATES:
            await self._forget(record, "cancelled", ALREADY_RUNNING)
            return
        if record.control == "shared" and session.control == "none":
            await self._forget(record, "dropped", TERMINAL_CLOSED)
            return
        record.at = FIRED
        self._store(record)
        session.resume = None
        await entry.channel.resume("fired")
        await entry.channel.publish_summary()
        try:
            await self.hub.send(
                {"session_id": record.session_id, "text": RESUME_PROMPT, "mode": "auto"},
                source="resume",
            )
        except RcError as exc:
            await self._forget(record, "dropped", exc.message)

    # --------------------------------------------------------------- records

    async def _remember(self, record: PendingResume, status: str) -> None:
        """Store a resume, publish its step and put it on the session."""
        entry = self.hub.entries.get(record.session_id)
        if entry is None:
            return
        self._records[record.session_id] = record
        self._store(record)
        entry.session.resume = record.to_wire()
        await entry.channel.resume(
            status,
            at=record.at,
            estimated=record.estimated,
            attempts=record.attempts if status == "rescheduled" else None,
        )
        await entry.channel.publish_summary()

    async def _forget(self, record: PendingResume, status: str | None, reason: str) -> None:
        """Drop a record, saying why when the apps are owed a row."""
        self._records.pop(record.session_id, None)
        self.registry.delete_resume(record.session_id)
        entry = self.hub.entries.get(record.session_id)
        if entry is None:
            return
        entry.session.resume = None
        if status is None:
            return
        await entry.channel.resume(status, reason=reason)
        await entry.channel.publish_summary()

    def _store(self, record: PendingResume) -> None:
        self.registry.save_resume(
            record.session_id,
            record.at,
            record.estimated,
            record.attempts,
            record.window_minutes,
            record.control,
        )
