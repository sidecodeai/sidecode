"""What `agy` says of one session, as it says it (A64).

Its `session/update`s are of two sorts. Some say what the session is: the context in use, the
commands it offers, its title, the options it runs with. The others are the conversation: text,
thoughts and tools, live during a turn or replayed by `session/load` before it answers. A live one
that comes when no turn is running is a turn `agy` began by itself — words, or a tool new to it —
and the context `agy` reports during such a turn is what ends it once nothing follows.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...models import Command
from ...sessions import titles
from ...sessions.channel import SessionChannel
from . import commands as commandlist
from . import publish
from .replay import AGENT, Record, Replay
from .translate import Translator, text_of
from .turnflow import TurnFlow
from .turns import note_context

# The updates a `session/load` replays.
REPLAYED = frozenset(
    {
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
    }
)
# The updates that say what options the session runs with.
OPTIONS = frozenset({"config_option_update", "current_mode_update"})

Commands = Callable[[list[Command]], None]
Options = Callable[[str, dict[str, Any]], Awaitable[None]]


class Stream:
    """Handles one session's updates, in the order `agy` sent them."""

    def __init__(
        self,
        channel: SessionChannel,
        *,
        turns: TurnFlow,
        translator: Translator,
        record: Record,
        on_commands: Commands,
        on_options: Options,
    ) -> None:
        self.channel = channel
        self._turns = turns
        self._translator = translator
        self._record = record
        self._on_commands = on_commands
        self._on_options = on_options
        # Set while a `session/load` replays the conversation.
        self.replay: Replay | None = None

    async def notification(
        self, method: str, params: dict[str, Any], session_id: str | None
    ) -> None:
        if method != "session/update":
            return
        named = params.get("sessionId")
        if session_id is not None and named not in (None, session_id):
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        kind = str(update.get("sessionUpdate") or "")
        if self.replay is not None and kind in REPLAYED:
            await self._replayed(kind, update)
        elif kind in REPLAYED:
            await self._live(kind, update)
        elif kind in OPTIONS:
            await self._on_options(kind, update)
        else:
            await self._state(kind, update)

    async def _state(self, kind: str, update: dict[str, Any]) -> None:
        """What `agy` says of the session rather than of the conversation."""
        if kind == "usage_update":
            note_context(self._turns.usage, update)
            turn = self._turns.turn
            if turn is not None and turn.wake:
                self._turns.quiet_soon()
        elif kind == "available_commands_update":
            self._on_commands(commandlist.advertised(update))
        elif kind == "session_info_update":
            title = update.get("title")
            if isinstance(title, str) and title:
                await titles.from_agent(self.channel, title)

    async def _live(self, kind: str, update: dict[str, Any]) -> None:
        if kind == "user_message_chunk":
            # `agy` echoes no prompt live; the device published the bubble itself.
            return
        turns = self._turns
        async with turns.lock:
            speaks = self._wakes(kind, update)
            if turns.turn is None and speaks:
                await turns.begin("agent")
            turn = turns.turn
            if turn is not None and turn.wake and speaks:
                # `agy` is at work again; a known tool's status says nothing of that.
                turns.cancel_quiet()
            if kind in ("tool_call", "tool_call_update"):
                emits, fresh = self._translator.tool(update)
                for emit in emits:
                    await publish.emit(self.channel, emit)
                if fresh:
                    self._record.note_tool(str(update.get("toolCallId") or ""))
                return
            await self._text(kind, text_of(update.get("content")))

    def _wakes(self, kind: str, update: dict[str, Any]) -> bool:
        """Whether this update begins a turn `agy` takes by itself: words, or a tool new to it."""
        if kind in ("tool_call", "tool_call_update"):
            return not self._translator.knows(str(update.get("toolCallId") or ""))
        return bool(text_of(update.get("content")))

    async def _text(self, kind: str, text: str) -> None:
        turn = self._turns.turn
        if turn is not None and turn.wake and not turn.spoken and kind == "agent_message_chunk":
            # A turn `agy` takes by itself goes on from its last words, after a blank line.
            text = text.lstrip("\n")
        if not text:
            return
        target = "assistant_text" if kind == "agent_message_chunk" else "thinking"
        emits, opened = self._translator.text(target, text)
        for emit in emits:
            await publish.emit(self.channel, emit)
        if target == "assistant_text":
            if turn is not None:
                turn.spoken = True
                turn.text += text
            if opened:
                self._record.note_text(AGENT)

    async def _replayed(self, kind: str, update: dict[str, Any]) -> None:
        walker = self.replay
        if walker is None:
            return
        if kind == "user_message_chunk":
            rows = walker.user(update.get("content"))
        elif kind == "agent_message_chunk":
            rows = walker.agent(text_of(update.get("content")))
        elif kind in ("tool_call", "tool_call_update"):
            rows = walker.tool(update)
        else:
            rows = []
        for row in rows:
            await publish.row(self.channel, row)
