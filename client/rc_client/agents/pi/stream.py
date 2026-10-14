"""One pi session's event stream, published as protocol events.

The device meets pi's agent events on two paths — the stdout of an RPC child it
started, and the socket an attached terminal's extension writes to — and they
are the same objects, so the translating, the turn bookkeeping and the steering
queue live here once and both paths own one of these.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...logging_setup import logger
from ...models import now_ms
from ...sessions.channel import SessionChannel
from ..base import Emit
from .translate import QUEUE, PiTranslator

log = logger("rc_client.pi.stream")

# A running tool's output is republished at most this often, so a noisy build
# cannot flood the link with replacement events.
OUTPUT_THROTTLE = 0.5

Usage = Callable[[], Awaitable[dict[str, Any] | None]]
TurnEndCallback = Callable[[], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class Steer:
    """A message sent into a running turn, and the block it is owed under."""

    text: str
    block_id: str


class PiStream:
    """Translates pi's events for one session and keeps that session's turn."""

    def __init__(
        self,
        channel: SessionChannel,
        *,
        usage: Usage,
        on_turn_end: TurnEndCallback | None = None,
    ) -> None:
        self.channel = channel
        self.trigger = "remote"
        self.streaming = False
        self.interrupting = False
        # A compaction an app asked for reports its own refusal in the reply to
        # `session.command`, so the notice for it is dropped rather than shown
        # twice (A27). Cleared by the `compaction_end` that follows.
        self.own_compaction = False
        self.done = asyncio.Event()
        self.done.set()
        self._usage = usage
        self._on_turn_end = on_turn_end
        self._translator = PiTranslator()
        self._started_at = 0
        self._steers: list[Steer] = []
        self._last_output_flush: dict[str, float] = {}

    # ------------------------------------------------------------------ turns

    async def begin(self, trigger: str) -> None:
        """Open a turn, unless one is already running."""
        if self.streaming:
            return
        self.trigger = trigger
        self.streaming = True
        self._started_at = now_ms()
        self.done.clear()
        await self.channel.begin_turn(trigger)

    async def finish(self, completion: dict[str, Any]) -> None:
        if self.channel.session.turn is None:
            self.streaming = False
            return
        reason = "interrupted" if self.interrupting else str(completion.get("stop_reason"))
        self.interrupting = False
        self.streaming = False
        self._last_output_flush.clear()
        await self._publish_unread(reason)
        duration = max(0, now_ms() - self._started_at)
        await self.channel.end_turn(reason, duration, await self._usage())
        self.done.set()
        if self._on_turn_end is not None:
            await self._on_turn_end()

    async def settle(self, timeout: float) -> bool:
        """Wait for the turn to end after an abort. False when it never does."""
        try:
            await asyncio.wait_for(self.done.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    # ----------------------------------------------------------------- events

    async def event(self, event: dict[str, Any]) -> None:
        """One of pi's agent events, verbatim."""
        if self._own_failure(event):
            return
        if str(event.get("type") or "") == "agent_start":
            # A turn somebody else started — a prompt typed in the terminal, a
            # retry pi began on its own — still opens one here.
            await self.begin(self.trigger)
        completion: dict[str, Any] | None = None
        for emit in self._translator.event(event):
            if emit.kind == "turn_completed":
                completion = dict(emit.fields)
                continue
            if emit.kind == QUEUE:
                await self._apply_queue(emit.fields)
                continue
            await self._apply(emit)
        if completion is not None:
            await self.finish(completion)

    def _own_failure(self, event: dict[str, Any]) -> bool:
        """True for the failure of a compaction this device asked for.

        A compaction that succeeds keeps its notice — that is what tells the
        apps earlier turns are now a summary — but one that pi refuses is
        already the message the command's caller is shown.
        """
        if not self.own_compaction or str(event.get("type") or "") != "compaction_end":
            return False
        self.own_compaction = False
        return bool(event.get("errorMessage") or event.get("aborted"))

    async def torn_down(self, message: str) -> None:
        """pi left while a turn was running: end it rather than stream for ever."""
        if self.channel.session.turn is None:
            self.streaming = False
            return
        for emit in self._translator.close_streams():
            await self._apply(emit)
        await self.channel.error(message)
        await self.finish({"stop_reason": "error"})

    async def _apply(self, emit: Emit) -> None:
        if emit.delta:
            fields = dict(emit.fields)
            block_id = str(fields.pop("block_id"))
            delta = str(fields.pop("delta", ""))
            await self.channel.emit_delta(emit.kind, block_id, delta, **fields)
            return
        if emit.kind == "tool_call" and emit.fields.get("status") == "running":
            block_id = str(emit.fields.get("block_id") or "")
            now = time.monotonic()
            if emit.fields.get("output") and now - self._last_output_flush.get(block_id, 0.0) < (
                OUTPUT_THROTTLE
            ):
                return
            self._last_output_flush[block_id] = now
        await self.channel.emit(emit.kind, **emit.fields)

    # --------------------------------------------------------------- steering

    def remember(self, steer: Steer) -> None:
        self._steers.append(steer)

    async def _apply_queue(self, fields: dict[str, Any]) -> None:
        """Publish the bubble of every steered message pi has now taken (A14).

        pi reports its whole steering queue whenever it changes, so a message
        that has left the queue is one the agent read, and that is where its
        `user_message` belongs. Nothing is taken while an interrupt is clearing
        the queue; those messages were never read and are published at the end
        of the turn instead.
        """
        if self.interrupting:
            return
        queued = list(fields.get("steering") or [])
        for steer in list(self._steers):
            if steer.text in queued:
                queued.remove(steer.text)
                continue
            self._steers.remove(steer)
            await self._publish_steer(steer)

    async def _publish_steer(self, steer: Steer) -> None:
        await self.channel.emit(
            "user_message", block_id=steer.block_id, text=steer.text, source="remote"
        )

    async def _publish_unread(self, stop_reason: str) -> None:
        """Show the steered messages this turn ended without ever reading."""
        unread = list(self._steers)
        self._steers.clear()
        for steer in unread:
            await self._publish_steer(steer)
            if stop_reason == "interrupted":
                await self.channel.notice(
                    "warn", "the agent was stopped before it read your message"
                )


def bubble_id(given: str | None = None) -> str:
    """The block a user message is drawn under: the app's own id where it gave one."""
    return given or f"user:{uuid.uuid4()}"
