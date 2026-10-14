"""Amendment A40: the device typing into an attached Claude Code terminal.

A Claude channel carries user text and nothing else, so a phone could never
change the model of a session running in a terminal, nor compact it. What it
can do instead is what the person would do: type `/model`, walk the picker with
the arrow keys and press `s`, which applies the choice to this session and
writes no settings file of theirs. The keystrokes go through the pseudo-terminal
the shim started the CLI inside (`rc_client.channel.pty`).

Three rules make that safe to do to somebody else's terminal.

- **Only into an idle terminal.** No turn, no dialog, an empty draft and a
  keyboard that has been still for two seconds; anything else is `conflict` and
  nothing is queued. A person mid-sentence must never have characters appear.
- **Never the argument form.** `/model opus` and `/effort low` apply at once
  but save the choice as the person's default. The pickers, confirmed with `s`,
  are the only route this file knows.
- **The transcript decides.** A script is done when the CLI has written the
  command and its answer, not when the keystrokes went out; until then the
  phone is told nothing and the session's `meta` says the old value.

Stopping a turn (amendment A42) is the one script those rules do not fit. Escape
is what the person would press, and a running turn is exactly when they want it
pressed, so it has no idle gate and waits for no confirmation: the CLI files
`[Request interrupted by user]` and the transcript reader ends the turn from
that (A32). What it does keep is the dialog rule — an approval or a question on
screen is answered, not escaped.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..errors import RcError
from ..logging_setup import logger
from .ptys import PtyLink

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry
    from .shared import SharedControl

log = logger("rc_client.typist")

BUSY = "the terminal is busy; try again in a moment"
REFUSED = "the terminal did not take the change"
# What a Stop says while the CLI is waiting on the person (A42).
ANSWER_FIRST = "answer the prompt first"

# How still the keyboard has to be before the device types.
QUIET_SECONDS = 2.0
# How long a picker has to appear, and how often the screen is read.
PICKER_TIMEOUT = 2.0
POLL_INTERVAL = 0.1
# How long the transcript has to show the command and its answer. The mirror
# reads the file every two seconds, so this is several passes.
CONFIRM_TIMEOUT = 8.0
# How long a typed command stays claimed, so its transcript row is known for
# ours however late the tailer gets to it.
ECHO_WINDOW = 30.0

UP = "\x1b[A"
DOWN = "\x1b[B"
LEFT = "\x1b[D"
RIGHT = "\x1b[C"
ESCAPE = "\x1b"
ENTER = "\r"
THIS_SESSION = "s"

MODEL_COMMAND = "/model"
EFFORT_COMMAND = "/effort"
COMPACT_COMMAND = "/compact"

# What the CLI prints when it has taken the change, matched loosely: the rest
# of the sentence says where it was saved, which is what we are avoiding.
MODEL_CONFIRMS = "set model"
EFFORT_CONFIRMS = "set effort"

# Our model ids against the words the picker draws. The rows carry more than
# the name ("Opus (1M context)", "Default (recommended)"), so only the first
# word is matched.
MODEL_ROWS = {
    "default": "Default",
    "opus": "Opus",
    "fable": "Fable",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
}
# The marker the TUI puts on the highlighted row (U+276F), written as an
# escape so nothing reflows it.
HIGHLIGHT = "\u276f"
_NAMES = "|".join(MODEL_ROWS.values())
# A row of the picker, as the full frame draws it: `1. Default (recommended)`.
_ROW = re.compile(rf"(\d)\.[ \t]+({_NAMES})\b")
# Where the highlight is. Moving it redraws that one row without its number
# (`> Opus (1M context)`), so the number is optional and the name decides.
_CURRENT = re.compile(rf"{HIGHLIGHT}[ \t\u00a0]{{0,4}}(?:\d\.[ \t]{{0,4}})?({_NAMES})\b")

# The slider's levels, left to right. It has one more than the device offers,
# so the walk is "all the way left, then right to the level asked for".
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
EFFORT_STEPS = 6
# The one line of the slider's footer that nothing else prints.
EFFORT_HINT = "s for this session only"
# On a conversation with cached history the CLI asks once more before it
# switches ("Switch model? … 1. Yes, switch to … 2. No, go back"), with the yes
# row highlighted. A fresh session never asks, so a scratch-home check never
# meets it. Enter takes the highlighted yes.
SWITCH_PROMPT = "Switch model?"
PROMPT_TIMEOUT = 1.5


@dataclass(slots=True)
class TypedCommand:
    """A command the device typed, waiting for the transcript to say it ran.

    `confirms` is the prefix of the CLI's own answer; a command that only
    needs its row claimed — `/compact`, whose outcome is the compaction the
    apps hear about anyway — leaves it empty.
    """

    command: str
    confirms: str = ""
    done: asyncio.Future[bool] | None = None
    seen: bool = False
    expires_at: float = field(default_factory=lambda: time.monotonic() + ECHO_WINDOW)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def resolve(self, ok: bool) -> None:
        if self.done is not None and not self.done.done():
            self.done.set_result(ok)


@dataclass(frozen=True, slots=True)
class Picker:
    """A numbered picker as the screen shows it, and the row it is sitting on."""

    rows: dict[int, str]
    current: str | None

    def row_for(self, name: str | None) -> int | None:
        if name is None:
            return None
        for number, label in self.rows.items():
            if label == name:
                return number
        return None


def read_picker(text: str) -> Picker:
    """Read the last state of a numbered picker out of the terminal's output.

    A TUI redraws, so the text holds every frame it has drawn: the last time a
    row was written is what is on screen now, and the last highlight marker is
    where the selection is.
    """
    rows = {int(number): name for number, name in _ROW.findall(text)}
    found = _CURRENT.findall(text)
    return Picker(rows=rows, current=found[-1] if found else None)


class Typist:
    """One script of keystrokes into one terminal, from one request."""

    def __init__(self, entry: SessionEntry, link: PtyLink, control: SharedControl) -> None:
        self.entry = entry
        self.link = link
        self.control = control

    # --------------------------------------------------------------- scripts

    async def set_model(self, choice: str) -> None:
        """`/model`, walk to the row, `s`: this session only, nothing saved."""
        name = MODEL_ROWS.get(choice)
        if name is None:
            raise RcError("bad_request", f"unknown model for claude: {choice}")
        await self._gate()
        pending = self.control.expect(self.entry, MODEL_COMMAND, MODEL_CONFIRMS)
        try:
            await self.link.keys(MODEL_COMMAND + ENTER, clear=True)
            await self._walk_to(name)
            await self.link.keys(THIS_SESSION, clear=True)
            await self._accept(SWITCH_PROMPT)
            await self._confirmed(pending)
        except RcError:
            await self._give_up(pending)
            raise
        await self.entry.channel.set_meta(model=choice)

    async def set_effort(self, level: str) -> None:
        """`/effort`, all the way left, right to the level, `s`."""
        if level not in EFFORT_ORDER:
            raise RcError("bad_request", f"unknown effort for claude: {level}")
        await self._gate()
        pending = self.control.expect(self.entry, EFFORT_COMMAND, EFFORT_CONFIRMS)
        try:
            await self.link.keys(EFFORT_COMMAND + ENTER, clear=True)
            await self._wait_for_text(EFFORT_HINT, "the effort slider did not open")
            await self.link.keys(LEFT * EFFORT_STEPS + RIGHT * EFFORT_ORDER.index(level))
            await self.link.keys(THIS_SESSION)
            await self._confirmed(pending)
        except RcError:
            await self._give_up(pending)
            raise
        await self.entry.channel.set_meta(effort=level)

    async def compact(self, block_id: str) -> None:
        """`/compact`: the command's own row in the transcript is our echo."""
        await self._gate()
        pending = self.control.expect(self.entry, COMPACT_COMMAND)
        try:
            await self.link.keys(COMPACT_COMMAND + ENTER)
        except RcError:
            self.control.unclaim(self.entry, pending)
            raise
        await self.entry.channel.emit(
            "user_message", block_id=block_id, text=COMPACT_COMMAND, source="remote"
        )

    async def interrupt(self) -> None:
        """A42: Escape, which is how a turn is stopped in that terminal.

        No idle gate: the point of Stop is a terminal that is not idle, and
        one Escape on Claude Code does not clear a half-typed line (it asks
        first). A dialog is the exception — it is answered, not escaped. What
        counts as a turn is `busy`, the same thing the apps are shown: an
        injection whose row has not reached the transcript yet is the CLI's
        turn already. With no turn at all there is nothing to stop.
        """
        state = self.entry.shared
        if state is None:
            raise RcError("conflict", "the session is no longer attached")
        if state.approvals or state.question is not None:
            raise RcError("conflict", ANSWER_FIRST)
        if not state.busy:
            return
        await self.link.keys(ESCAPE)

    # ----------------------------------------------------------------- gates

    async def _gate(self) -> None:
        """Nothing is typed into a terminal somebody else is using."""
        state = self.entry.shared
        if state is None or not state.injectable:
            raise RcError("conflict", BUSY)
        terminal = await self.link.state()
        if terminal.draft > 0 or terminal.idle_for < QUIET_SECONDS:
            raise RcError("conflict", BUSY)

    async def _walk_to(self, name: str) -> None:
        """Move the highlight onto the row, and make sure it landed there."""
        picker = await self._wait_for_picker()
        target = picker.row_for(name)
        here = picker.row_for(picker.current)
        if target is None or here is None:
            raise RcError("conflict", REFUSED)
        distance = target - here
        if distance:
            key = DOWN if distance > 0 else UP
            await self.link.keys(key * abs(distance))
            if (await self._wait_for_picker(on=name)).current != name:
                raise RcError("conflict", REFUSED)

    async def _wait_for_picker(self, on: str | None = None) -> Picker:
        """Read the screen until the rows are there, and on the row asked for."""
        deadline = time.monotonic() + PICKER_TIMEOUT
        picker = Picker(rows={}, current=None)
        while time.monotonic() < deadline:
            picker = read_picker((await self.link.screen()).text)
            drawn = bool(picker.rows) and picker.current is not None
            if drawn and (on is None or picker.current == on):
                return picker
            await asyncio.sleep(POLL_INTERVAL)
        if not picker.rows or picker.current is None:
            raise RcError("conflict", "the model picker did not open")
        return picker

    async def _accept(self, prompt: str) -> None:
        """Press Enter on a confirmation the CLI may put up, if it does.

        The prompt's yes row is the highlighted one, so Enter is the answer;
        a CLI that asks nothing is simply not waited on past the timeout.
        """
        deadline = time.monotonic() + PROMPT_TIMEOUT
        while time.monotonic() < deadline:
            if prompt in (await self.link.screen()).text:
                await self.link.keys(ENTER)
                return
            await asyncio.sleep(POLL_INTERVAL)

    async def _wait_for_text(self, needle: str, failure: str) -> None:
        deadline = time.monotonic() + PICKER_TIMEOUT
        while time.monotonic() < deadline:
            if needle in (await self.link.screen()).text:
                return
            await asyncio.sleep(POLL_INTERVAL)
        raise RcError("conflict", failure)

    async def _confirmed(self, pending: TypedCommand) -> None:
        """Wait for the CLI's own record that the change happened."""
        future = pending.done
        if future is None:
            return
        try:
            taken = await asyncio.wait_for(asyncio.shield(future), timeout=CONFIRM_TIMEOUT)
        except TimeoutError as exc:
            raise RcError("conflict", REFUSED) from exc
        if not taken:
            raise RcError("conflict", REFUSED)

    async def _give_up(self, pending: TypedCommand) -> None:
        """Close whatever is open and leave the terminal as it was found."""
        self.control.unclaim(self.entry, pending)
        try:
            await self.link.keys(ESCAPE)
        except RcError:
            log.warning("could not close the dialog the device opened", pid=self.link.pid)
