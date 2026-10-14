"""Per-session event publication.

Everything a session says to the gateway goes through here so that `seq`
allocation, delta coalescing, size bounds, persistence and the `Session`
summary stay consistent with one another.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..events import BLOCK_KINDS, DELTA_FLUSH_MS, bound_event, should_store
from ..ids import block_uuid
from ..models import UNSET, Session, SessionState, now_ms
from ..registry import Registry
from .limits import LimitStop

Publisher = Callable[[dict[str, Any]], Awaitable[None]]
# What a finished turn reports to whoever schedules resumes: the session it
# belongs to, and the usage limit that ended it when one did (amendment A35).
TurnEndHook = Callable[[Session, LimitStop | None], Awaitable[None]]
FLUSH_INTERVAL = DELTA_FLUSH_MS / 1000.0
_TOKEN_COUNTS = ("input_tokens", "output_tokens", "total_tokens")


def _has_counts(usage: dict[str, Any]) -> bool:
    """True when a usage payload actually measured something."""
    return any(int(usage.get(key) or 0) > 0 for key in _TOKEN_COUNTS)


class SessionChannel:
    def __init__(self, registry: Registry, session: Session, publish: Publisher) -> None:
        self.registry = registry
        self.session = session
        self._publish = publish
        self._lock = asyncio.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._flusher: asyncio.Task[None] | None = None
        self._closed = False
        # Set while the session is being closed on the person's word (A39). The
        # agent's last words arrive during it — a turn ending, a status going
        # idle — and none of them is a session coming back to life.
        self.closing = False
        # Set by the hub to the resume scheduler (amendment A35, 7.2). A channel
        # nobody wired one into simply ends its turns.
        self.on_turn_end: TurnEndHook | None = None
        # Seeded from the registry so a resumed session keeps the ordering its
        # already-delivered blocks were given.
        self._first_seq: dict[str, int] = registry.first_seqs(session.session_id)

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def close(self) -> None:
        self._closed = True
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
            self._flusher = None
        await self.flush_all()

    # ---------------------------------------------------------------- events

    async def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Publish one non-streaming event, flushing that block's deltas first."""
        block_id = fields.get("block_id")
        if isinstance(block_id, str):
            await self.flush_block(block_id)
        async with self._lock:
            return await self._write(kind, fields)

    async def emit_delta(self, kind: str, block_id: str, delta: str, **fields: Any) -> None:
        """Buffer a streaming append; flushed at most every 80 ms per block."""
        if not delta:
            return
        async with self._lock:
            entry = self._pending.get(block_id)
            if entry is None:
                entry = {"kind": kind, "delta": "", "fields": dict(fields), "due": 0.0}
                self._pending[block_id] = entry
            entry["delta"] = str(entry["delta"]) + delta
        self.start()

    async def flush_block(self, block_id: str) -> None:
        async with self._lock:
            entry = self._pending.pop(block_id, None)
            if entry is None:
                return
            fields = dict(entry["fields"])
            fields["block_id"] = block_id
            fields["delta"] = entry["delta"]
            # The schema requires `done` on both streaming kinds; a delta is by
            # definition not the final event for its block.
            fields["done"] = False
            await self._write(str(entry["kind"]), fields)

    async def flush_all(self) -> None:
        for block_id in list(self._pending):
            await self.flush_block(block_id)

    async def _flush_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(FLUSH_INTERVAL)
            if self._pending:
                await self.flush_all()

    async def _write(self, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        seq = self.registry.next_seq(self.session.session_id)
        payload = dict(fields)
        for key in ("block_id", "parent_block_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                payload[key] = block_uuid(value)
        block_id = payload.get("block_id")
        if isinstance(block_id, str) and kind in BLOCK_KINDS:
            # Amendment A8: blocks stay where they first appeared, even when a
            # long-running one is replaced much later.
            payload["first_seq"] = self._first_seq.setdefault(block_id, seq)
        full = {"seq": seq, "ts": now_ms(), "kind": kind, **payload}
        wire = bound_event(full)
        self.session.last_seq = seq
        if should_store(wire):
            self.registry.store_event(self.session.session_id, wire, full)
        await self._publish(
            {"type": "session.event", "session_id": self.session.session_id, "event": wire}
        )
        return wire

    # ------------------------------------------------------------ summaries

    async def publish_summary(self) -> None:
        self.session.updated_at = now_ms()
        self.registry.upsert_session(self.session)
        await self._publish({"type": "session.updated", "session": self.session.to_dict()})

    async def set_state(self, state: SessionState, detail: str | None = None) -> None:
        if state == "running":
            # Amendment A15: a turn is under way, wherever it was started from.
            await self.revive()
        if self.session.state == state and self.session.state_detail == detail:
            return
        self.session.state = state
        self.session.state_detail = detail
        fields: dict[str, Any] = {"state": state}
        if detail:
            fields["detail"] = detail
        await self.emit("status", **fields)
        await self.publish_summary()

    async def revive(self, state: SessionState = "idle") -> None:
        """Amendment A15: a session that is working again is not archived.

        `hub.load` reads an archived session back as `stopped`, which is true
        right up to the moment something drives it; leaving that behind would
        show a live session as a dead one. Publishing the summary is what moves
        the row out of the Archive in the apps, so it is not optional.

        A session being closed is the one thing this never speaks for (A39):
        what the agent says while it is being stopped is the end of it, not a
        session coming back to life.
        """
        if self.closing or not self.session.archived:
            return
        self.session.archived = False
        if self.session.state == "stopped":
            self.session.state = state
            self.session.state_detail = None
        await self.publish_summary()

    async def set_meta(self, **fields: Any) -> None:
        """Apply a partial `Session` update and mirror it as a `meta` event.

        A field the caller did not mention is `UNSET`, or simply absent. `None`
        is a value, which is how a session goes back to its agent's standard
        speed (amendment A21).
        """
        changed: dict[str, Any] = {}
        for key, value in fields.items():
            if value is UNSET:
                continue
            if getattr(self.session, key, None) != value:
                setattr(self.session, key, value)
                changed[key] = value
        if not changed:
            return
        await self.emit("meta", **changed)
        await self.publish_summary()

    async def notice(self, level: str, text: str) -> None:
        await self.emit("notice", level=level, text=text)

    async def error(self, message: str, code: str | None = None) -> None:
        fields: dict[str, Any] = {"message": message}
        if code:
            fields["code"] = code
        await self.emit("error", **fields)

    async def resume(
        self,
        status: str,
        at: int | None = None,
        estimated: bool | None = None,
        attempts: int | None = None,
        reason: str | None = None,
    ) -> None:
        """One step of a resume after a usage limit, as a timeline row (A35, 5.15)."""
        fields: dict[str, Any] = {"status": status}
        if at is not None:
            fields["at"] = at
        if estimated is not None:
            fields["estimated"] = estimated
        if attempts is not None:
            fields["attempts"] = attempts
        if reason:
            fields["reason"] = reason
        await self.emit("resume", **fields)

    async def publish_queue(self, pending: list[dict[str, Any]]) -> None:
        self.session.queued = len(pending)
        await self.emit("queue", pending=pending)
        await self.publish_summary()

    async def publish_todos(self, items: list[dict[str, Any]]) -> None:
        await self.emit("todos", items=items)
        done = sum(1 for item in items if item.get("status") == "completed")
        self.session.todos = {"total": len(items), "done": done} if items else None
        await self.publish_summary()

    # ----------------------------------------------------------------- turns

    async def begin_turn(self, trigger: str) -> str:
        await self.revive()
        turn_id = str(uuid.uuid4())
        self.session.turn = {"turn_id": turn_id, "started_at": now_ms()}
        await self.emit("turn_started", turn_id=turn_id, trigger=trigger)
        await self.set_state("running")
        return turn_id

    async def end_turn(
        self,
        stop_reason: str,
        duration_ms: int,
        usage: dict[str, Any] | None = None,
        limit: LimitStop | None = None,
    ) -> None:
        """End the turn, saying so it was the vendor's usage limit when it was.

        `limit` is only ever set beside `stop_reason: "error"` (A35, 5.9), and
        the hook that follows is what schedules the session's resume.
        """
        turn = self.session.turn or {}
        turn_id = str(turn.get("turn_id") or uuid.uuid4())
        fields: dict[str, Any] = {
            "turn_id": turn_id,
            "stop_reason": stop_reason,
            "duration_ms": duration_ms,
        }
        if limit is not None:
            fields["limit"] = limit.to_dict()
        if usage and _has_counts(usage):
            merged = dict(self.session.usage or {})
            merged.update(usage)
            self.session.usage = merged
            fields["usage"] = merged
        elif self.session.usage:
            # An interrupted turn reports zeros for everything it never billed.
            # Overwriting with those would erase the session's real totals.
            fields["usage"] = dict(self.session.usage)
        self.session.turn = None
        await self.flush_all()
        await self.emit("turn_completed", **fields)
        await self.set_state("idle")
        if self.on_turn_end is not None:
            await self.on_turn_end(self.session, limit)
