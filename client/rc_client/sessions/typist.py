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
  phone is told nothing and the session's `meta` says the old value. The
  answer is also what the session then says: where the model or the person's
  settings cap the effort, the CLI takes a lower level than the one asked for,
  and the session reports that one.

`/rename` (amendment A55) is the one script that types an argument, because it
saves nothing but the session's own name. It keeps the idle gate, and what
decides it is the transcript too: the CLI's own `custom-title` row, not its
wording. A name the prompt box would read more into than text is never typed.

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
from .ptys import PtyLink, TerminalGone, TerminalSilent
from .rename import normalise

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry
    from .shared import SharedControl

log = logger("rc_client.typist")

BUSY = "the terminal is busy; try again in a moment"
REFUSED = "the terminal did not take the change"
# What a rename that went unconfirmed says (A55): the keystrokes may have landed.
NOT_CONFIRMED = "the terminal did not confirm the new name"
# And what every script says after one, until the person has used the terminal.
UNCONFIRMED = (
    "a rename typed here was not confirmed and may still be in the prompt; use the terminal first"
)
# And one the device cannot type into at all: the CLI is open in a terminal whose
# keyboard is not the device's, so there is nothing to type a rename into.
NOT_TYPEABLE = "the session is open in a terminal this device cannot type into; rename it there"
# What a Stop says while the CLI is waiting on the person (A42).
ANSWER_FIRST = "answer the prompt first"

# How still the keyboard has to be before the device types.
QUIET_SECONDS = 2.0
# How long a picker has to appear, and how often the screen is read.
PICKER_TIMEOUT = 2.0
POLL_INTERVAL = 0.1
# How long the transcript has to show the command and its answer. The mirror
# reads the file every two seconds, and once more the moment the wait begins,
# finding the transcript of a session that had none until now, so this is
# several passes.
CONFIRM_TIMEOUT = 8.0
# How long a typed command stays claimed, so its transcript row is known for
# ours however late the tailer gets to it.
ECHO_WINDOW = 30.0
# How often a rename asks the transcript reader to look again while it waits,
# and how long the rows it leaves are still taken for its own once it is done.
RENAME_POLL = 0.4
RENAME_GRACE = 3.0
# How much later than it was, by the clocks of both ends, the person's last key has
# to be before it counts as a key since, and how many characters of a rename's text
# are looked for on the screen to tell whether it is still in the prompt box.
HUMAN_SLACK = 1.0
RESIDUE_CHARS = 12

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
RENAME_COMMAND = "/rename"

# What the CLI prints when it has taken the change, matched loosely: the rest
# of the sentence says where it was saved, which is what we are avoiding.
MODEL_CONFIRMS = "set model"
# Every answer to `/effort` names it, and says which level it set: the one asked
# for, or the nearest one the model or the person's settings allow. A slider that
# stops short says so the first way below, a cap in their settings the second
# ("… exceeds the cap …; set to 'high' instead"), and that level is what the
# session then reports.
EFFORT_CONFIRMS = "effort"
_EFFORT_TAKEN = re.compile(r"set effort level to ([a-z]+)|set to '([a-z]+)' instead")

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

# The slider's levels, left to right. Claude Code 2.1.278 drew one more past
# them; 2.1.299 draws these five, fewer for a model whose effort is capped, and
# keeps Ultracode apart as a toggle on Tab, which the device never presses. Both
# stop at their ends, so the walk is "all the way left, then right to the level
# asked for", and a capped slider stops short, which its answer then says.
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
EFFORT_STEPS = 6
# What only the open slider draws, old and new. The TUI rewrites only the cells
# that changed and the screen text marks each jump with one space, so a word the
# screen already half showed reads "se sion": any one of these whole will do.
EFFORT_HINTS = ("\u2190/\u2192 to adjust", "s for this session only", "Smarter", "Ultracode")
# On a conversation with cached history the CLI asks once more before it
# switches ("Switch model? … 1. Yes, switch to … 2. No, go back"), with the yes
# row highlighted. A fresh session never asks, so a scratch-home check never
# meets it. Enter takes the highlighted yes. 2.1.299 asks the same about a new
# effort level, under a title of its own.
SWITCH_PROMPT = "Switch model?"
EFFORT_PROMPTS = ("Change effort level?", "Yes, switch to")
PROMPT_TIMEOUT = 1.5


@dataclass(slots=True)
class Unconfirmed:
    """A rename nobody confirmed, which may have left its text in the prompt box (A55).

    Enter may not have submitted it, and whatever the device types next would
    land after it. The terminal is not the device's to type into until the person
    has used it, a turn has started or the attachment has ended; no key is sent
    to clean up. `human_at` is when the person last touched the keyboard on the
    monotonic clock, as far as the device could tell: the proxy counts only
    their keys, never the device's own.
    """

    human_at: float

    def moved(self, idle_for: float) -> bool:
        """Whether the person has used the keyboard since, given how long it has been quiet."""
        return time.monotonic() - idle_for > self.human_at + HUMAN_SLACK


@dataclass(slots=True)
class TypedCommand:
    """A command the device typed, waiting for the transcript to say it ran.

    `confirms` is what the CLI's own answer says; a command that only needs
    its row claimed — `/compact`, whose outcome is the compaction the apps hear
    about anyway — leaves it empty. `done` gets the whole answer, lower case,
    or `None` when the CLI said something else or the script gave up. A rename
    is `by_title`: what it waits for is the CLI's `custom-title` row, and `done`
    gets the name that row carries (A55).
    """

    command: str
    confirms: str = ""
    done: asyncio.Future[str | None] | None = None
    seen: bool = False
    by_title: bool = False
    expires_at: float = field(default_factory=lambda: time.monotonic() + ECHO_WINDOW)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def resolve(self, answer: str | None) -> None:
        if self.done is not None and not self.done.done():
            self.done.set_result(answer)


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


def _shows(text: str, marks: tuple[str, ...]) -> bool:
    """Whether the screen shows any of these, whole."""
    return any(mark in text for mark in marks)


def effort_taken(answer: str) -> str:
    """The level an `/effort` answer says the CLI set, which has to be one we know."""
    found = _EFFORT_TAKEN.search(answer)
    level = (found.group(1) or found.group(2)) if found is not None else None
    if level is None or level not in EFFORT_ORDER:
        raise RcError("conflict", REFUSED)
    return level


def typeable(name: str) -> bool:
    """Whether a name can be typed after `/rename ` without the prompt box reading more into it.

    `@` opens the box's file completion, where Enter may pick a suggestion
    instead of ending the line, and a trailing backslash makes Enter insert a
    newline. Neither has been seen on a screen here: the box is known to do
    both, and a rename is not worth finding out. The name has been through
    `sessions.rename.normalise`, so it holds no control character that could
    end the line early.
    """
    return "@" not in name and not name.endswith("\\")


def may_hold(screen: str, name: str) -> bool:
    """Whether a typed `/rename <name>` may still be in the prompt box: its start is on screen.

    A wrapped line and the box's own borders break the text up, so the spaces
    are left out and only the first words are looked for. The command it echoes
    into the conversation looks the same, and counts too: nothing says which
    it is, and the cost of being wrong that way is a keystroke from the person.
    """
    first = "".join(f"{RENAME_COMMAND}{name}".split())[: len(RENAME_COMMAND) + RESIDUE_CHARS]
    return first in "".join(screen.split())


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
            await self._accept((SWITCH_PROMPT,))
            await self._confirmed(pending)
        except RcError:
            await self._give_up(pending)
            raise
        await self.entry.channel.set_meta(model=choice)

    async def set_effort(self, level: str) -> None:
        """`/effort`, all the way left, right to the level, `s`.

        The session reports the level the CLI's answer names, which a capped
        model, or a cap in the person's settings, makes lower than the one asked
        for.
        """
        if level not in EFFORT_ORDER:
            raise RcError("bad_request", f"unknown effort for claude: {level}")
        await self._gate()
        pending = self.control.expect(self.entry, EFFORT_COMMAND, EFFORT_CONFIRMS)
        try:
            await self.link.keys(EFFORT_COMMAND + ENTER, clear=True)
            await self._wait_for_text(EFFORT_HINTS, "the effort slider did not open")
            await self.link.keys(LEFT * EFFORT_STEPS + RIGHT * EFFORT_ORDER.index(level))
            await self.link.keys(THIS_SESSION, clear=True)
            await self._accept(EFFORT_PROMPTS)
            taken = effort_taken(await self._confirmed(pending))
        except RcError:
            await self._give_up(pending)
            raise
        await self.entry.channel.set_meta(effort=taken)

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

    async def rename(self, name: str) -> str:
        """`/rename <name>`: the name the CLI took, from its own `custom-title` row (A55).

        The argument form, which A40 forbids for `/model` because it saves the
        person's default: `/rename` saves nothing but this session's name, and
        has no picker to walk. The caller has checked that the name is
        `typeable`. The codes say what is known. A busy terminal, or one that
        may hold the text of an earlier rename, is `conflict` and nothing is
        typed; one the device cannot reach is `unsupported`; one that goes quiet
        before anything is typed is `timeout`. Once the keys have gone, no
        confirmation is `timeout` as well, and the terminal is looked at for the
        text, because Enter may not have submitted it. No Escape follows a
        script that opened no dialog.
        """
        if normalise(name) != name:
            # Text that becomes keystrokes holds no control character: one could end the line.
            raise RcError("bad_request", "the name must be normalised before it is typed")
        try:
            await self._gate()
        except TerminalGone as exc:
            raise RcError("unsupported", NOT_TYPEABLE) from exc
        except TerminalSilent as exc:
            raise RcError("timeout", exc.message) from exc
        pending = self.control.expect(self.entry, RENAME_COMMAND, by_title=True)
        self.control.quiet_for(self.entry, CONFIRM_TIMEOUT + RENAME_GRACE)
        try:
            await self.link.keys(f"{RENAME_COMMAND} {name}{ENTER}", clear=True)
        except TerminalGone as exc:
            self.control.unclaim(self.entry, pending)
            raise RcError("unsupported", NOT_TYPEABLE) from exc
        except RcError as exc:
            self.control.unclaim(self.entry, pending)
            await self._check_for_text(name)
            raise RcError("timeout", NOT_CONFIRMED) from exc
        return await self._named(pending, name)

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
        if state.unconfirmed is not None:
            if not state.unconfirmed.moved(terminal.idle_for):
                raise RcError("conflict", UNCONFIRMED)
            state.unconfirmed = None
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

    async def _accept(self, prompts: tuple[str, ...]) -> None:
        """Press Enter on a confirmation the CLI may put up, if it does.

        The prompt's yes row is the highlighted one, so Enter is the answer;
        a CLI that asks nothing is simply not waited on past the timeout.
        """
        deadline = time.monotonic() + PROMPT_TIMEOUT
        while time.monotonic() < deadline:
            if _shows((await self.link.screen()).text, prompts):
                await self.link.keys(ENTER)
                return
            await asyncio.sleep(POLL_INTERVAL)

    async def _wait_for_text(self, needles: tuple[str, ...], failure: str) -> None:
        deadline = time.monotonic() + PICKER_TIMEOUT
        while time.monotonic() < deadline:
            if _shows((await self.link.screen()).text, needles):
                return
            await asyncio.sleep(POLL_INTERVAL)
        raise RcError("conflict", failure)

    async def _confirmed(self, pending: TypedCommand) -> str:
        """Wait for the CLI's own record that the change happened, and return it.

        The rows are being written now, so the transcript is read at once
        rather than at the next pass; a session that had no transcript yet has
        one from this command on, and it is looked for as well.
        """
        future = pending.done
        if future is None:
            return ""
        self.control.read_soon()
        try:
            answer = await asyncio.wait_for(asyncio.shield(future), timeout=CONFIRM_TIMEOUT)
        except TimeoutError as exc:
            raise RcError("conflict", REFUSED) from exc
        if answer is None:
            raise RcError("conflict", REFUSED)
        return answer

    async def _named(self, pending: TypedCommand, name: str) -> str:
        """Wait for the `custom-title` row the CLI writes, and return the name it carries.

        The transcript is asked for again every `RENAME_POLL` rather than at the
        reader's own pace, so the wait is the CLI's and not the two-second tail.
        """
        future = pending.done
        if future is None:
            raise RcError("timeout", NOT_CONFIRMED)
        deadline = time.monotonic() + CONFIRM_TIMEOUT
        while time.monotonic() < deadline:
            self.control.read_soon()
            try:
                taken = await asyncio.wait_for(asyncio.shield(future), timeout=RENAME_POLL)
            except TimeoutError:
                continue
            if taken:
                return taken
            break
        self.control.unclaim(self.entry, pending)
        await self._check_for_text(name)
        raise RcError("timeout", NOT_CONFIRMED)

    async def _check_for_text(self, name: str) -> None:
        """After a rename nobody confirmed, look for its text in the prompt box, typing nothing.

        If it may be there, or the screen cannot be read, the terminal is marked
        `Unconfirmed` and the log says so once; `_gate` does the rest.
        """
        state = self.entry.shared
        if state is None:
            return
        try:
            seen = await self.link.screen()
        except RcError:
            human_at = time.monotonic()
        else:
            if not may_hold(seen.text, name):
                return
            human_at = time.monotonic() - seen.idle_for
        state.unconfirmed = Unconfirmed(human_at)
        log.warning(
            "a typed rename was not confirmed and may still be in the prompt", pid=self.link.pid
        )

    async def _give_up(self, pending: TypedCommand) -> None:
        """Close whatever is open and leave the terminal as it was found."""
        self.control.unclaim(self.entry, pending)
        try:
            await self.link.keys(ESCAPE)
        except RcError:
            log.warning("could not close the dialog the device opened", pid=self.link.pid)
