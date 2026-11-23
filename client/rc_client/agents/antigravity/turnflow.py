"""The turn under way in one Antigravity session, the prompts in it, and how it ends (A64).

A message is one `session/prompt`, and the turn ends when the prompt that owns it answers: the last
one sent into it, since a message sent while a turn runs is another prompt `agy` takes the turn
over with, answering the earlier one at once — which is when the steered message's bubble goes out.
A turn `agy` begins by itself has no prompt: it ends a few seconds after `agy` last reported the
context with nothing after it. However a turn ends, every card it left open expires, the session's
tokens are summed into it, and a quota message makes the device ask whether a window is used up.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ...attachments import drop
from ...errors import RcError
from ...models import now_ms
from ...sessions.channel import SessionChannel
from ...sessions.limits import LimitStop
from . import errors, publish
from .acp import AgyError
from .dialogs import Dialogs
from .replay import USER, Record
from .translate import Translator, stop_reason
from .turns import REFUSED, Prompt, Turn, add_usage

UNREAD = "the agent was stopped before it read your message"

# Whether a turn's words and error read as a quota stop, and the limit `agy`'s query then shows.
LimitCheck = Callable[[str, str], Awaitable[LimitStop | None]]
After = Callable[[], Awaitable[None]]
Quiet = Callable[[], float]
Run = Callable[[Prompt], Coroutine[Any, Any, None]]


class TurnFlow:
    """The turn and its prompts; everything that changes them holds `lock`."""

    def __init__(
        self,
        channel: SessionChannel,
        *,
        translator: Translator,
        dialogs: Dialogs,
        record: Record,
        usage: dict[str, Any],
        hooks: Hooks,
    ) -> None:
        self.channel = channel
        self._translator = translator
        self._dialogs = dialogs
        self._record = record
        self._hooks = hooks
        # What the session has spent through the device, and the context as `agy` last said.
        self.usage = usage
        self.turn: Turn | None = None
        self.prompts: list[Prompt] = []
        # The prompt whose answer ends the turn under way: the last one sent into it.
        self.owner: Prompt | None = None
        self.interrupting = False
        self.lock = asyncio.Lock()
        self._quiet: asyncio.Task[None] | None = None
        self._after: set[asyncio.Task[None]] = set()

    @property
    def busy(self) -> bool:
        return self.turn is not None

    # ------------------------------------------------------------- beginnings

    async def begin(self, trigger: str) -> None:
        self.turn = Turn(wake=trigger == "agent")
        await self.channel.begin_turn(trigger)

    async def wake_if_idle(self) -> None:
        """`agy` asks something during a turn it began by itself."""
        async with self.lock:
            if self.turn is None:
                await self.begin("agent")

    async def announce(self, prompt: Prompt) -> None:
        """The message's bubble, where it stands in the conversation; once."""
        if prompt.announced:
            return
        fields: dict[str, Any] = {
            "block_id": prompt.block_id,
            "text": prompt.text,
            "source": prompt.source,
        }
        if prompt.shown:
            fields["attachments"] = prompt.shown
        await self.channel.emit("user_message", **fields)
        prompt.announced = True
        self._record.note_text(USER)

    def launch(self, prompt: Prompt, run: Run) -> None:
        prompt.task = asyncio.create_task(run(prompt), name="agy-prompt")
        self.prompts.append(prompt)
        self.owner = prompt

    async def unread(self) -> None:
        """End the turn a message began and never sent, if it got that far."""
        async with self.lock:
            if self.turn is None or self.owner is not None:
                return
            turn, self.turn = self.turn, None
            await self.channel.end_turn("interrupted", turn.duration)
        await self.channel.notice("warn", UNREAD)

    # ---------------------------------------------------------------- answers

    async def answered(self, prompt: Prompt, result: dict[str, Any]) -> None:
        async with self.lock:
            add_usage(self.usage, result.get("usage"))
            if await self._superseded(prompt) or prompt is not self.owner:
                return
            await self.end(stop_reason(str(result.get("stopReason") or "")))

    async def failed(self, prompt: Prompt, exc: RcError) -> None:
        async with self.lock:
            code = exc.rpc_code if isinstance(exc, AgyError) else None
            if prompt.steer and not prompt.announced:
                # `agy` took no steer: a card opened meanwhile. Nothing of it reached `agy`.
                self._forget(prompt)
                if self.owner is prompt:
                    self.owner = self.prompts[-1] if self.prompts else None
                prompt.settle(False)
                await drop(prompt.written)
                return
            if await self._superseded(prompt) or prompt is not self.owner:
                return
            if code in REFUSED:
                await drop(prompt.written)
            await self.end("error", exc.message)

    def _forget(self, prompt: Prompt) -> None:
        if prompt in self.prompts:
            self.prompts.remove(prompt)

    async def _superseded(self, prompt: Prompt) -> bool:
        """Take an answered prompt off the list; True when a later one carries the turn on.

        The later prompt's bubble goes out now, since this answer is `agy` taking it over.
        """
        later = self.prompts[self.prompts.index(prompt) + 1 :] if prompt in self.prompts else []
        self._forget(prompt)
        if not later:
            return False
        successor = later[0]
        await self.announce(successor)
        successor.settle(True)
        return True

    async def cancel_prompts(self) -> None:
        """The process is going: no prompt in it will answer."""
        for prompt in list(self.prompts):
            if prompt.task is not None:
                prompt.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt.task
        self.forget_prompts()

    def forget_prompts(self) -> None:
        self.prompts.clear()
        self.owner = None

    # ----------------------------------------------------------------- ending

    async def end(self, reason: str, error: str | None = None) -> None:
        """End the turn under way; the caller holds the lock."""
        turn = self.turn
        if turn is None:
            return
        self.cancel_quiet()
        for emit in self._translator.close_streams():
            await publish.emit(self.channel, emit)
        if error:
            turn.error = errors.words(error)
            await self.channel.error(turn.error)
        stop = "interrupted" if self.interrupting else reason
        found = await self._hooks.limit(turn.text, turn.error)
        await self._dialogs.expire_all()
        self._record.ledger.wrote = now_ms()
        self._record.save()
        self.turn = None
        self.owner = None
        self.interrupting = False
        await self.channel.end_turn(
            "error" if found else stop, turn.duration, dict(self.usage) or None, found
        )
        self.spawn(self._hooks.after())

    def spawn(self, work: Awaitable[None]) -> None:
        task = asyncio.ensure_future(work)
        self._after.add(task)
        task.add_done_callback(self._after.discard)

    # -------------------------------------------- a turn agy began by itself

    def quiet_soon(self) -> None:
        """`agy` reported the context: a turn it began by itself ends if nothing follows."""
        self.cancel_quiet()
        self._quiet = asyncio.create_task(self._quiet_end(), name="agy-wake-quiet")

    def cancel_quiet(self) -> None:
        quiet, self._quiet = self._quiet, None
        if quiet is not None and quiet is not asyncio.current_task():
            quiet.cancel()

    async def _quiet_end(self) -> None:
        await asyncio.sleep(self._hooks.quiet())
        async with self.lock:
            turn = self.turn
            if turn is None or not turn.wake or self._dialogs.pending:
                return
            self._quiet = None
            await self.end("completed")


class Hooks:
    """What a turn's end asks of the session around it."""

    def __init__(self, limit: LimitCheck, after: After, quiet: Quiet) -> None:
        self.limit = limit
        self.after = after
        self.quiet = quiet
