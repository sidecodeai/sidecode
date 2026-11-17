"""One board update, from its trigger to the stored board and the frames it sends (A62).

In order, stopping at the first step that says no: the board is still followed, the account's
task board is on and the session is still the account's; a board that has not fallen behind needs
nothing, and one in a back-off or over the hourly cap waits. Then the policy is asked, before the
device is read; the device must be connected; the history is read, one read per device at a time,
and the window chosen. A window with nothing new stores the cursors and calls nothing. Otherwise
the model is asked, at most `CONCURRENT_CALLS` at once across the gateway, and its answer checked
and merged, and the board stored under the store's next revision. A failure leaves the board and
its cursors as they were, so the same rounds are read again next time, and earns a back-off. An
update whose board was deleted meanwhile, or whose account turned the board off, writes nothing and
sends nothing more.

Each update writes one log line, `board run`, with ids and counts and never anything a person or an
agent wrote, nor the model's answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..logging import logger
from .context import Context, RunTicket, queue_hash
from .follow import Follow
from .history import Frame, History, read_history
from .lanes import Request
from .llm import BoardModel, Completion, ModelFailure
from .merge import merge
from .pacing import backoff_until
from .policy import BoardRefusal
from .prompt import messages, parse
from .store import BoardRow
from .window import queued_lines, render, select
from .wire import (
    DEVICE_OFFLINE,
    HISTORY_UNAVAILABLE,
    INVALID_OUTPUT,
    MODEL_FAILED,
    RATE_LIMITED,
    STATUS_CURRENT,
    STATUS_UPDATING,
    Status,
    board_view,
    stale,
    unavailable,
)

log = logger("rc_gateway.board")

UPDATED = "updated"
UNCHANGED = "unchanged"
NOTHING_NEW = "nothing_new"


@dataclass
class _Record:
    """What one update's log line says, and the ticket it writes and sends under."""

    ticket: RunTicket
    follow: Follow
    trigger: str
    watched: bool
    started: float
    items_before: int
    outcome: str = ""
    events: int = 0
    rounds_new: int = 0
    rounds_context: int = 0
    window_chars: int = 0
    items_after: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def write(self) -> None:
        fields: dict[str, object] = {
            "session_id": self.follow.session_id,
            "device_id": self.follow.device_id,
            "username": self.follow.username,
            "trigger": self.trigger,
            "watched": self.watched,
            "outcome": self.outcome,
            "events": self.events,
            "rounds_new": self.rounds_new,
            "rounds_context": self.rounds_context,
            "window_chars": self.window_chars,
            "items_before": self.items_before,
            "items_after": self.items_before if self.items_after is None else self.items_after,
        }
        if self.input_tokens is not None:
            fields["input_tokens"] = self.input_tokens
        if self.output_tokens is not None:
            fields["output_tokens"] = self.output_tokens
        fields["ms"] = int((time.monotonic() - self.started) * 1000)
        log.info("board run", **fields)


class Runner:
    def __init__(self, context: Context, *, called: Callable[[str], None]) -> None:
        self._ctx = context
        #: Tells the session's lane its model call starts now, which spaces the next one.
        self._called = called
        self._device_locks: dict[str, asyncio.Lock] = {}

    def forget_device(self, device_id: str) -> None:
        self._device_locks.pop(device_id, None)

    async def run(self, session_id: str, request: Request) -> None:
        ctx = self._ctx
        follow = ctx.followed.get(session_id, ctx.now_ms())
        if follow is None:
            return
        ticket = ctx.begin_run(session_id, follow.username)
        try:
            await self._run(ticket, follow, request)
        finally:
            ctx.end_run(ticket)

    async def _run(self, ticket: RunTicket, follow: Follow, request: Request) -> None:
        ctx = self._ctx
        row = await self._target(follow)
        if row is not None and request.rebuild:
            row = await self._start_over(ticket, row)
        if row is None:
            return
        if not await ctx.behind(ticket.session_id, row):
            await self._send_status(ticket, STATUS_CURRENT)
            return
        held = ctx.held_back(row, force=request.force)
        if held is not None:
            await self._send_status(ticket, held)
            return
        record = _Record(
            ticket=ticket,
            follow=follow,
            trigger=request.trigger,
            watched=ctx.watchers.watched(ticket.session_id),
            started=time.monotonic(),
            items_before=len(row.items),
        )
        try:
            await self._update(record, row)
        finally:
            if record.outcome:
                record.write()

    # ---- what an update may still do: write and send only while its ticket stands ----

    async def _write_board(self, ticket: RunTicket, row: BoardRow) -> BoardRow | None:
        if not self._ctx.running(ticket):
            return None
        return await self._ctx.store.write_board(row)

    async def _write_state(self, ticket: RunTicket, row: BoardRow) -> bool:
        if not self._ctx.running(ticket):
            return False
        return await self._ctx.store.write_state(row)

    async def _send_status(self, ticket: RunTicket, status: Status) -> None:
        if self._ctx.running(ticket):
            await self._ctx.watchers.publish_status(ticket.session_id, status)

    async def _send_board(self, ticket: RunTicket, row: BoardRow, status: Status) -> None:
        if self._ctx.running(ticket):
            await self._ctx.watchers.publish_board(ticket.session_id, board_view(row), status)

    async def _target(self, follow: Follow) -> BoardRow | None:
        """The board's row, while the account's board is on and the session still the account's."""
        ctx = self._ctx
        if not (await ctx.preferences.get(follow.username)).board_on:
            return None
        account = await ctx.host.session_account(follow.session_id)
        row = await ctx.store.get(follow.session_id)
        if account is None or account[1] != follow.username:
            ctx.followed.drop(follow.session_id)
            await ctx.store.remove(follow.session_id)
            return None
        if row is None or row.username != follow.username:
            ctx.followed.drop(follow.session_id)
            return None
        return row

    async def _start_over(self, ticket: RunTicket, row: BoardRow) -> BoardRow | None:
        """Clear the items and the cursors, keeping the id counter; then the update reads anew."""
        cleared = row.cleared()
        written: BoardRow | None = None
        if row.revision > 0:
            written = await self._write_board(ticket, cleared)
        elif await self._write_state(ticket, cleared):
            written = cleared
        if written is not None:
            await self._send_board(ticket, written, STATUS_UPDATING)
        return written

    async def _update(self, record: _Record, row: BoardRow) -> None:
        ctx = self._ctx
        session_id, device_id = row.session_id, row.device_id
        try:
            model = await ctx.policy.admit(row.username)
        except BoardRefusal as refusal:
            await self._refused(record, refusal.reason)
            return
        except ModelFailure:
            await self._failed(record, row, MODEL_FAILED)
            return
        retry_at = ctx.hourly.retry_at(row.username, ctx.now_ms())
        if retry_at is not None:
            record.outcome = RATE_LIMITED
            await self._send_status(record.ticket, stale(RATE_LIMITED, retry_at))
            return
        if not ctx.host.device_online(device_id):
            await self._offline(record)
            return
        await self._send_status(record.ticket, STATUS_UPDATING)
        indexed = await ctx.sessions.get(session_id)
        checked = indexed.last_seq if indexed is not None else row.checked_seq
        snapshot = ctx.host.queue_snapshot(session_id)
        history = await self._read(row)
        if history is None:
            if ctx.host.device_online(device_id):
                await self._failed(record, row, HISTORY_UNAVAILABLE)
            else:
                await self._offline(record)
            return
        await self._apply(record, row, model, history, checked, snapshot)

    async def _apply(
        self,
        record: _Record,
        row: BoardRow,
        model: BoardModel,
        history: History,
        checked: int,
        snapshot: Frame | None,
    ) -> None:
        """Choose the window from what was read, and ask the model about it if anything is new."""
        ctx = self._ctx
        window = select(
            history.events, reached_start=not history.has_more, through_seq=row.through_seq
        )
        queued = queued_lines(snapshot)
        rendered = render(window, queued)
        record.events = len(history.events)
        record.rounds_new = rendered.rounds_new
        record.rounds_context = rendered.rounds_context
        record.window_chars = len(rendered.text)
        cursors = replace(
            row,
            through_seq=max(row.through_seq, history.highest_seq),
            checked_seq=checked,
            queue_hash=queue_hash(snapshot),
            failures=0,
            retry_at=None,
        )
        if not window.has_new and (cursors.queue_hash == row.queue_hash or not queued):
            if await self._write_state(record.ticket, cursors):
                ctx.failures.pop(row.session_id, None)
                record.outcome = NOTHING_NEW
                await self._send_status(record.ticket, STATUS_CURRENT)
            return
        completion = await self._ask(record, row, model, rendered.text)
        if completion is None:
            return
        answer = parse(completion.text, row.items)
        if answer is None:
            await self._failed(record, row, INVALID_OUTPUT)
            return
        now = ctx.now_ms()
        items, next_item = merge(row.items, answer, next_item=row.next_item, now=now)
        board = replace(
            cursors,
            items=items,
            next_item=next_item,
            partial=row.partial or window.partial or rendered.partial,
            updated_at=now,
        )
        saved = await self._write_board(record.ticket, board)
        if saved is None:
            return
        ctx.failures.pop(row.session_id, None)
        record.items_after = len(items)
        record.outcome = UPDATED if items != row.items else UNCHANGED
        await self._send_board(record.ticket, saved, STATUS_CURRENT)

    async def _read(self, row: BoardRow) -> History | None:
        lock = self._device_locks.setdefault(row.device_id, asyncio.Lock())
        async with lock:
            return await read_history(
                self._ctx.host.ask_device, row.device_id, row.session_id, row.through_seq
            )

    async def _ask(
        self, record: _Record, row: BoardRow, model: BoardModel, conversation: str
    ) -> Completion | None:
        ctx = self._ctx
        failure: BoardRefusal | ModelFailure | None = None
        completion: Completion | None = None
        async with ctx.calls:
            self._called(row.session_id)
            ctx.hourly.record(row.username, ctx.now_ms())
            try:
                completion = await model.complete(messages(row.items, conversation))
            except (BoardRefusal, ModelFailure) as exc:
                failure = exc
        if isinstance(failure, BoardRefusal):
            await self._refused(record, failure.reason)
        elif failure is not None:
            await self._failed(record, row, MODEL_FAILED)
        if completion is not None:
            record.input_tokens = completion.input_tokens
            record.output_tokens = completion.output_tokens
        return completion

    async def _refused(self, record: _Record, reason: str) -> None:
        """The policy will not have the account's boards updated: said for a minute, no back-off."""
        ctx = self._ctx
        ctx.verdicts.put(record.follow.username, reason, ctx.now_ms())
        record.outcome = f"refused:{reason}"
        await self._send_status(record.ticket, unavailable(reason))

    async def _offline(self, record: _Record) -> None:
        """The device is away: the board waits for its next hello, turn end or open."""
        record.outcome = DEVICE_OFFLINE
        await self._send_status(record.ticket, stale(DEVICE_OFFLINE))

    async def _failed(self, record: _Record, row: BoardRow, reason: str) -> None:
        """A failure: the board and its cursors stay, and the next try waits a back-off."""
        ctx = self._ctx
        failures = row.failures + 1
        retry_at = backoff_until(failures, ctx.now_ms())
        if not await self._write_state(
            record.ticket, replace(row, failures=failures, retry_at=retry_at)
        ):
            return
        ctx.failures[row.session_id] = reason
        record.outcome = reason
        await self._send_status(record.ticket, stale(reason, retry_at))
