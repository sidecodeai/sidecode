"""Answering Claude Code's usage-limit menu from an app (amendment A63).

Claude Code continues a turn the usage limit stopped by itself, at most three
times after the person last typed; a message from a phone does not count as
typing, so a session driven from one meets the fourth stop with a menu in its
terminal, and waits there, whatever the phone sends, until somebody chooses.
This file is the device's side of that menu, for a session it is attached to.

- **Seeing it.** The main thread's `StopFailure` hook with `rate_limit`, the
  transcript's 429 row when no hook said so, or an injection the CLI queued
  where the device took it for idle, starts one look of at most
  `DETECT_WINDOW` at the screen of the terminal the device owns
  (`limit_menu.py` reads it). No menu means Claude Code goes on by itself.
- **Showing it.** The free options on screen and Close become a `question`
  block an app may also answer in words, the session reports `needs_input`
  and the gateway's push follows. While the menu is up nothing is injected and
  nothing is typed; a message waits in the device's queue, in Up next.
- **Escape is the device's own key.** A message from the person, a written
  answer or Close: with the menu still on screen and the keyboard still for
  two seconds, a lone Escape, which closes the menu and leaves Claude Code's
  own continue as it was. The message then goes, a minute after the limit
  resets when that is still to come.
- **A row is the person's to pick.** An option an app chose is found again on
  screen by its words and pressed as its number; one whose words are no longer
  there is refused, and the card shows the menu as it is now. The one row the
  device picks itself is "Wait here", found the same way, when the account
  resumes after the limit (A35).
- **What it cannot read** it does not touch: a menu it cannot read, or one on a
  terminal it does not own, is a notice and `needs_input`, and no key at all.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..agents.claude import live
from ..errors import RcError
from ..logging_setup import logger
from . import limit_menu
from .limit_menu import Menu, Row
from .limits import LimitStop
from .ptys import PtyLink, TerminalGone, TerminalState

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry
    from .shared import SharedControl, SharedState

log = logger("rc_client.limit")

# How long the screen is looked at for the menu after a stop, and how often.
DETECT_WINDOW = 5.0
DETECT_POLL = 0.25
# How often an open menu is looked at for an answer the terminal gave.
WATCH_POLL = 1.0
# How still the keyboard is before the device presses Escape, or "Wait here" (A35).
QUIET = 2.0
# Claude Code refuses a choice made within 250 ms of drawing the menu or of a key.
SETTLE = 0.4
STEADY_TRIES = 5
# How long the screen has to show that a key closed the menu, and how often it is read.
CONFIRM_WAIT = 1.5
CONFIRM_POLL = 0.1
# How much later than a look the person's last key has to be to count as one since.
HUMAN_SLACK = 0.5
# A stop older than this is one a transcript read only now, long after.
RECENT_MS = 10 * 60 * 1000
# A held message goes this long after the limit resets, as the resume of A35 does, so it
# does not meet the limit again on a clock that is a little ahead.
RESET_LEAD_MS = 60 * 1000
# How long after that the session is looked at again.
WAKE_MARGIN = 1.0
# How much of the screen's end a key waits on: the proxy types it only while the
# screen still ends that way, so a menu that closed meanwhile takes no key.
TAIL_CHARS = 200
# How long the rows of a choice the device pressed are taken for its own.
ROWS_WINDOW = 30.0

ESCAPE = "\x1b"
CLOSE = "close"
WAIT = "wait"
QUESTION_ID = "q0"
# What Claude Code records for a choice in the menu, and what it says once "Wait here" is
# taken: the transcript's own word that the CLI will go on by itself.
MENU_COMMAND = "/rate-limit-options"
CONTINUES = "continue automatically"
# Whose messages close the menu: the person's, and the resume they asked for (A35).
PERSON_SOURCES = frozenset({"remote", "resume"})

LIMIT_REACHED = "Claude Code reached its usage limit."
WAITING = (
    "Claude Code is waiting in its terminal for you to choose what to do. "
    "Anything you write here instead closes the menu and is sent as a message."
)
CLOSE_LABEL = "Close"
CLOSE_DESCRIPTION = "Close the menu, as Esc does in the terminal."
NOTICE = (
    "Claude Code is waiting in its terminal for a choice about the usage limit; answer it there."
)
FAILED = (
    "The usage-limit menu could not be answered from here; answer it in Claude Code's terminal."
)
AUTO = "Chose “{label}” in Claude Code's usage-limit menu, as resuming after the limit is on."
BUSY = "the terminal is busy; try again in a moment"
CHANGED = "the menu in the terminal has changed; choose again"


class Mode(Enum):
    LOOKING = "looking"
    DRIVEN = "driven"
    NOTICED = "noticed"


class Outcome(Enum):
    CHOSEN = "chosen"
    ELSEWHERE = "elsewhere"
    GONE = "gone"
    BUSY = "busy"
    FAILED = "failed"
    CHANGED = "changed"


@dataclass(slots=True)
class Card:
    """The `question` block an app answers the menu through."""

    request_id: str
    block_id: str
    question: dict[str, Any]


@dataclass(slots=True)
class LimitEpisode:
    """One usage-limit stop of an attached session, and the menu after it.

    `hooked` and `written` say which signals reported the stop: the hook and
    the transcript row of one stop are one episode, whichever comes first,
    since the CLI cannot stop again before it has gone on, which ends the
    episode. `limited` is false for a look an injection the CLI queued started,
    with no stop reported: that one is said only if the menu is read. `asks`
    is whether a card or a notice waits on the person, which is what
    `needs_input` says. `recorded` is set once Claude Code's own record has
    said a dialog is open during it, after which the record saying otherwise is
    the menu gone. `typing` is set while the device presses a key, so nothing
    reads the menu closing under it as the terminal's answer, and `went` is
    what tells it the menu went meanwhile: the CLI taking a prompt or saying it
    will continue.
    """

    said: str = ""
    resets_at_ms: int | None = None
    hooked: bool = False
    written: bool = False
    limited: bool = True
    mode: Mode = Mode.LOOKING
    menu: Menu | None = None
    card: Card | None = None
    auto: bool = False
    asks: bool = False
    recorded: bool = False
    opened_at: float = 0.0
    looked_at: float = 0.0
    typing: bool = False
    went: bool = False
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def holds(self) -> bool:
        """Whether messages wait for it: while the device drives it, and the look after a stop.

        A menu the device cannot read holds nothing back: the CLI keeps what it
        is sent behind its menu, and runs it the moment the person answers. Nor
        does a look a queued injection started, which any dialog causes, until
        it has read the usage-limit menu.
        """
        return self.mode is Mode.DRIVEN or (self.mode is Mode.LOOKING and self.limited)


class LimitDialogs:
    """The usage-limit menus of the sessions `SharedControl` is attached to."""

    def __init__(self, control: SharedControl) -> None:
        self.control = control

    # ------------------------------------------------------------- signals

    async def hooked(self, entry: SessionEntry, said: str) -> None:
        """The main thread's `StopFailure` hook said `rate_limit`: the turn stopped at the limit."""
        state = entry.shared
        if state is None:
            return
        episode = state.limit
        if episode is not None and not episode.hooked and (episode.written or not episode.limited):
            # The transcript's row of this stop came first, or a queued injection started the look.
            episode.hooked = episode.limited = True
            episode.said = episode.said or said
            await self._refresh_card(entry, episode)
            return
        await self._begin(entry, LimitEpisode(said=said, hooked=True))

    async def stopped(self, entry: SessionEntry, limit: LimitStop) -> None:
        """The transcript's 429 row: the stop, the time it resets and the CLI's sentence.

        The reset holds back what waits however late the row is read; only a
        row written a while ago opens no menu.
        """
        state = entry.shared
        if state is None:
            return
        state.limit_until_ms = limit.resets_at_ms
        self._wake_at_reset(entry, limit.resets_at_ms)
        if not self._recent(limit):
            return
        episode = state.limit
        if episode is not None and episode.hooked and not episode.written:
            episode.written = True
            episode.resets_at_ms = limit.resets_at_ms
            if limit.said and limit.said != episode.said:
                episode.said = limit.said
                await self._refresh_card(entry, episode)
            return
        if episode is not None and not episode.limited and not episode.written:
            # The look an injection the CLI queued started was this stop's.
            episode.limited = episode.written = True
            episode.said, episode.resets_at_ms = limit.said, limit.resets_at_ms
            await self._refresh_card(entry, episode)
            return
        await self._begin(
            entry, LimitEpisode(said=limit.said, resets_at_ms=limit.resets_at_ms, written=True)
        )

    async def blocked(self, entry: SessionEntry) -> None:
        """The CLI queued an injection the device made into what it took for an idle CLI.

        Something on screen holds the CLI's prompt, and any dialog does that;
        the screen is looked at for the usage-limit menu, and nothing else is
        said of it.
        """
        state = entry.shared
        if state is None or state.limit is not None:
            return
        await self._begin(entry, LimitEpisode(limited=False))

    async def moved_on(self, entry: SessionEntry) -> None:
        """The CLI took a prompt or began a turn: no menu is up, and no limit holds anything."""
        state = entry.shared
        if state is None:
            return
        state.limit_until_ms = None
        episode = state.limit
        if episode is None:
            return
        if episode.typing:
            episode.went = True
            return
        await self._end(entry, episode, by="terminal")

    async def command_output(self, entry: SessionEntry, text: str) -> None:
        """What the CLI printed for a command: after "Wait here", that it will go on by itself.

        That is the transcript's own word that the menu went and that the CLI
        resumes after the reset, which is when the device's own resume would
        be a second one (A35, 7.2).
        """
        state = entry.shared
        if state is None or CONTINUES not in text.lower():
            return
        episode = state.limit
        if episode is not None:
            if episode.typing:
                episode.went = True
            else:
                await self._end(entry, episode, by="terminal")
        await self.control.continues_by_itself(entry)

    def claims(self, state: SharedState, command: str) -> bool:
        """Whether a typed-command row is the menu's own, for a choice the device pressed."""
        return command == MENU_COMMAND and time.monotonic() < state.menu_rows_until

    # ------------------------------------------------------------ requests

    def answers(self, state: SharedState, request_id: str) -> bool:
        """Whether this request id is the card of the menu the session waits on."""
        episode = state.limit
        card = episode.card if episode is not None else None
        return card is not None and card.request_id == request_id

    def drives(self, state: SharedState, source: str) -> bool:
        """Whether a message from `source` is the device's to close the menu for (A63).

        Only the person's own, and the resume they asked for: a message an
        agent sends never closes a menu that waits on a person.
        """
        episode = state.limit
        return source in PERSON_SOURCES and episode is not None and episode.mode is Mode.DRIVEN

    async def answer(self, entry: SessionEntry, request_id: str, answers: dict[str, Any]) -> None:
        """An app's answer to the card: Close, a row, or words to send (A63, 6.3)."""
        async with entry.lock:
            state = entry.shared
            episode = state.limit if state is not None else None
            if state is None or episode is None or not self.answers(state, request_id):
                # Answered meanwhile, in the terminal or by another app.
                return
            value = answers.get(QUESTION_ID)
            offered = _offered(episode)
            if isinstance(value, list):
                # A choice is a list of option ids (5.8), and this menu takes one of its own.
                if len(value) != 1 or str(value[0]) not in offered:
                    raise RcError("bad_request", "that is not an option this menu offers")
                await self._picked(entry, episode, str(value[0]), offered[str(value[0])])
                return
            words = value.strip() if isinstance(value, str) else ""
            if not words:
                raise RcError("bad_request", "that is not an answer this menu takes")
            # Words are the person's message: Escape closes the menu, and they go as one.
            await self.control.hub.resumes.cancelled_by_person(entry.session.session_id)
            await self.control.send(
                entry, str(uuid.uuid4()), words, [], "remote", answer={QUESTION_ID: words}
            )

    async def make_way(self, entry: SessionEntry, answers: dict[str, Any] | None = None) -> None:
        """The person's message closes the menu with Escape, and goes as any other (A63).

        The caller holds the lock. A terminal whose keyboard is in use, or a
        screen that no longer shows the menu for certain, keeps the menu and its
        card, and the message waits in the queue.
        """
        state = entry.shared
        episode = state.limit if state is not None else None
        if episode is None or episode.mode is not Mode.DRIVEN:
            return
        outcome = await self._escape(entry, episode)
        if outcome is Outcome.CHOSEN:
            closed = answers or ({QUESTION_ID: [CLOSE]} if episode.card is not None else None)
            await self._end(entry, episode, by="remote", answers=closed)
        elif outcome in (Outcome.ELSEWHERE, Outcome.GONE):
            await self._settled_by(entry, episode, outcome, CLOSE)

    # ----------------------------------------------------------- lifecycle

    async def expire(self, entry: SessionEntry, state: SharedState) -> None:
        """The CLI is gone, or another took its place: nobody can answer its menu now."""
        episode, state.limit = state.limit, None
        if episode is None:
            return
        _stop(episode)
        if episode.card is not None:
            await _emit(entry, episode.card, status="expired")

    @staticmethod
    def drop(state: SharedState) -> None:
        """Forget the menu without a word: the session itself is going."""
        episode, state.limit = state.limit, None
        if episode is not None:
            _stop(episode)

    # ------------------------------------------------------------- looking

    async def _begin(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        state = entry.shared
        if state is None:
            return
        previous = state.limit
        if previous is not None:
            # The CLI went on past the last menu, which the device missed, and stopped again.
            await self._end(entry, previous, by="terminal")
        state.limit = episode
        await self.control.hold_steers(entry)
        await self.control.limit_changed(entry)
        episode.task = asyncio.get_running_loop().create_task(
            self._run(entry, episode), name="limit-menu"
        )

    async def _run(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        try:
            if await self._look(entry, episode):
                await self._watch(entry, episode)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the usage-limit menu could not be followed")
            if self._current(entry, episode):
                await self._end(entry, episode)

    async def _look(self, entry: SessionEntry, episode: LimitEpisode) -> bool:
        """Look for the menu for `DETECT_WINDOW`; whether there is one to watch after.

        A menu read whole is driven. After a stop, one whose title or footer is
        up but whose rows cannot be read, and one Claude Code's own record says
        is open on a terminal the device does not own, are said and left alone.
        Nothing at all means Claude Code is waiting to go on by itself.
        """
        deadline = time.monotonic() + DETECT_WINDOW
        shown = False
        while self._current(entry, episode):
            link = self.control.link_for(entry)
            if link is not None:
                text = await _screen(link)
                menu = limit_menu.read_menu(text) if text is not None else None
                if menu is not None:
                    return await self._drive(entry, episode, menu)
                shown = shown or (text is not None and limit_menu.shows_dialog(text))
            elif episode.limited and self._record(entry, episode) is True:
                return await self._notice(entry, episode)
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(DETECT_POLL)
        if not self._current(entry, episode):
            return False
        if episode.limited and (shown or self._record(entry, episode) is True):
            return await self._notice(entry, episode)
        await self._end(entry, episode)
        return False

    async def _drive(self, entry: SessionEntry, episode: LimitEpisode, menu: Menu) -> bool:
        """The menu is up and readable: a card for it, or, with resuming on, "Wait here" soon."""
        async with entry.lock:
            if not self._current(entry, episode):
                return False
            episode.mode = Mode.DRIVEN
            episode.menu = menu
            episode.opened_at = episode.looked_at = time.monotonic()
            episode.auto = self._wants_wait(episode)
            if not episode.auto:
                await self._ask(entry, episode)
            await self.control.limit_changed(entry)
        log.info("the usage-limit menu is up", rows=len(menu.rows), auto=episode.auto)
        return True

    async def _notice(self, entry: SessionEntry, episode: LimitEpisode) -> bool:
        """A menu the device cannot read or reach: said once, and never typed into."""
        async with entry.lock:
            if not self._current(entry, episode):
                return False
            episode.mode = Mode.NOTICED
            await _say(entry, episode, NOTICE)
            await self.control.limit_changed(entry)
        log.info("a usage-limit menu the device cannot drive is up")
        return True

    async def _ask(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        """The card: the free options on screen, Close, and room for a message."""
        request_id = str(uuid.uuid4())
        episode.card = Card(
            request_id=request_id, block_id=f"limit:{request_id}", question=_question(episode)
        )
        episode.asks = True
        await _emit(entry, episode.card, status="pending")

    async def _refresh_card(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        """The card says what the CLI and its menu say now: its sentence, its rows."""
        card = episode.card
        if card is None:
            return
        card.question = _question(episode)
        await _emit(entry, card, status="pending")

    # ------------------------------------------------------------ watching

    async def _watch(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        """Follow an open menu until it goes: an answer, the terminal's, or the terminal going.

        The terminal is only read after the person has pressed a key there,
        since nothing else closes the menu; Claude Code's own record, where it
        says, is read every time.
        """
        while True:
            await asyncio.sleep(WATCH_POLL)
            if not self._current(entry, episode):
                return
            if episode.typing:
                continue
            record = self._record(entry, episode)
            if record is False:
                await self._closed_elsewhere(entry, episode)
                return
            link = self.control.link_for(entry)
            if link is None:
                if episode.mode is Mode.DRIVEN:
                    async with entry.lock:
                        await self._settled_by(entry, episode, Outcome.GONE, CLOSE)
                    return
                if record is None:
                    # Nothing is left to watch: the CLI going on, or going, ends it.
                    return
                continue
            try:
                terminal = await link.state()
            except RcError:
                continue
            if _keyed_since(terminal.idle_for, episode.looked_at):
                text = await _screen(link)
                episode.looked_at = time.monotonic()
                if text is not None and _gone(text, episode):
                    await self._closed_elsewhere(entry, episode)
                    return
            if terminal.idle_for >= QUIET and (episode.auto or self._wants_wait(episode)):
                await self._quiet_menu(entry, episode)

    async def _closed_elsewhere(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        async with entry.lock:
            if self._current(entry, episode) and not episode.typing:
                await self._end(entry, episode, by="terminal")

    def _wants_wait(self, episode: LimitEpisode) -> bool:
        """Whether the account resumes after the limit and the menu shows "Wait here" whole (A35).

        A row whose words the screen broke is never the device's to pick.
        """
        return (
            episode.mode is Mode.DRIVEN
            and _wait_label(episode) is not None
            and self.control.resumes_after_limit()
        )

    async def _quiet_menu(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        """The keyboard is still: "Wait here", which Claude Code then does by itself (A63).

        Resuming after the limit, turned off since the menu came up, or a
        "Wait here" the device cannot find whole by its words, leaves the
        choice to the person, and the card goes up.
        """
        async with entry.lock:
            if not self._current(entry, episode):
                return
            outcome = Outcome.CHANGED
            if self._wants_wait(episode):
                outcome = await self._choose(entry, episode, WAIT, _wait_label(episode))
            if outcome is Outcome.BUSY:
                return
            if outcome is Outcome.CHOSEN:
                label = _wait_label(episode) or WAIT
                await entry.channel.notice("info", AUTO.format(label=label))
            if outcome in (Outcome.CHANGED, Outcome.FAILED):
                if episode.auto and self._current(entry, episode):
                    episode.auto = False
                    await self._ask(entry, episode)
                    await self.control.limit_changed(entry)
                return
            await self._settled_by(entry, episode, outcome, WAIT)

    # ------------------------------------------------------------- choosing

    async def _picked(
        self, entry: SessionEntry, episode: LimitEpisode, picked: str, label: str
    ) -> None:
        """Close, or a row the person picked, which the screen must still show in those words."""
        outcome = (
            await self._escape(entry, episode)
            if picked == CLOSE
            else await self._choose(entry, episode, picked, label)
        )
        if outcome is Outcome.BUSY:
            raise RcError("conflict", BUSY)
        if outcome is Outcome.CHANGED:
            # The person picked from a menu that is not on screen any more: they see this one.
            await self._refresh_card(entry, episode)
            raise RcError("conflict", CHANGED)
        await self._settled_by(entry, episode, outcome, picked)

    async def _escape(self, entry: SessionEntry, episode: LimitEpisode) -> Outcome:
        """A lone Escape, which closes the menu and changes nothing else in Claude Code.

        Only with the menu still on screen, and the person's keyboard still for
        `QUIET`: an Escape in a terminal somebody is using, or after the menu
        closed, would land on whatever is there instead.
        """
        return await self._keyed(entry, episode, None, "")

    async def _choose(
        self, entry: SessionEntry, episode: LimitEpisode, kind: str, label: str | None
    ) -> Outcome:
        """Press the number of the row that still says `label`, as the screen shows it now."""
        if label is None:
            return Outcome.CHANGED
        return await self._keyed(entry, episode, kind, label)

    async def _keyed(
        self, entry: SessionEntry, episode: LimitEpisode, kind: str | None, label: str
    ) -> Outcome:
        """Read the menu again and press its key, Escape for no `kind`; then see it close.

        The caller holds the session's lock. A menu the screen no longer shows
        is the terminal's answer when it was drawn over after a key of the
        person's, and otherwise one the device cannot answer.
        """
        link = self.control.link_for(entry)
        if link is None:
            return Outcome.GONE
        episode.typing = True
        episode.went = False
        try:
            return await self._press(entry, episode, link, kind, label)
        except TerminalGone:
            return Outcome.GONE
        except RcError:
            return Outcome.FAILED
        finally:
            episode.typing = False

    async def _press(
        self,
        entry: SessionEntry,
        episode: LimitEpisode,
        link: PtyLink,
        kind: str | None,
        label: str,
    ) -> Outcome:
        """Find the key on a screen that has held still, press it on that screen, see it close.

        The key goes on the condition that the screen still ends as it was read,
        so a menu that closes in the moment between takes no key meant for it.
        A key the menu ignored drew nothing, and is pressed once more.
        """
        found: Menu | None = None
        key = ESCAPE
        for attempt in range(2):
            seen = await _steady(link)
            if seen is None:
                return Outcome.BUSY if attempt == 0 else Outcome.FAILED
            if found is None:
                now = self._menu_now(entry, episode, seen)
                if not isinstance(now, Menu):
                    return now
                found = episode.menu = now
                if kind is not None:
                    row = _row_saying(found, kind, label)
                    if row is None or row.digit is None:
                        return Outcome.CHANGED
                    key = row.digit
                elif seen.idle_for < QUIET:
                    return Outcome.BUSY
            elif limit_menu.foreign(limit_menu.screen_lines(seen.text), found.vocabulary):
                # The first key closed it, only later than it was looked for.
                return Outcome.CHOSEN
            if kind is not None:
                # What the CLI records for this choice is the device's, not a command typed.
                self.control.claim_menu_rows(entry, ROWS_WINDOW)
            typed = await link.keys_if_shown(key, _tail(seen.text), clear=True)
            if not typed.sent:
                return Outcome.CHANGED if kind is not None else Outcome.BUSY
            self.control.read_soon()
            closed = await self._confirm(entry, episode, link, found)
            if closed is not False:
                return Outcome.CHOSEN if closed else Outcome.FAILED
            log.info("the usage-limit menu did not take a key", attempt=attempt + 1)
        return Outcome.FAILED

    def _menu_now(
        self, entry: SessionEntry, episode: LimitEpisode, seen: TerminalState
    ) -> Menu | Outcome:
        """The menu as the screen shows it now, or what its absence means.

        Once the person has used the keyboard, the menu has to be the last thing
        drawn; until then nothing could have closed it, whatever else was drawn.
        """
        menu = limit_menu.find_menu(seen.text)
        touched = _keyed_since(seen.idle_for, episode.opened_at)
        if menu is not None and (menu.last or not touched):
            return menu
        if (touched and _gone(seen.text, episode)) or self._record(entry, episode) is False:
            return Outcome.ELSEWHERE
        return Outcome.FAILED

    async def _confirm(
        self, entry: SessionEntry, episode: LimitEpisode, link: PtyLink, menu: Menu
    ) -> bool | None:
        """Whether the key closed the menu: True, False for a key it swallowed, None unknown.

        The screen was cleared as the key went, so what is on it now is what the
        CLI drew since. Something foreign to the menu is the CLI moving on, as
        are the CLI taking a prompt, its saying it will continue by itself, and
        Claude Code's own record no longer saying a dialog is open once it said
        one was. Nothing at all, or only the menu again, is a key it ignored,
        which is pressed once more. Anything else is left alone: a key typed
        past a closed menu would land in the prompt.
        """
        deadline = time.monotonic() + CONFIRM_WAIT
        known = menu.vocabulary
        text = ""
        while True:
            await asyncio.sleep(CONFIRM_POLL)
            if episode.went or self._record(entry, episode) is False:
                return True
            text = (await link.screen()).text
            if limit_menu.foreign(limit_menu.screen_lines(text), known):
                return True
            if time.monotonic() >= deadline:
                break
        if not text.strip() or limit_menu.words(text) or self._record(entry, episode) is True:
            return False
        return None

    async def _settled_by(
        self, entry: SessionEntry, episode: LimitEpisode, outcome: Outcome, picked: str
    ) -> None:
        """What a key ended as, for the card, the session and the person."""
        if outcome is Outcome.CHOSEN:
            card = episode.card
            offered = card is not None and picked in _offered(episode)
            answers = {QUESTION_ID: [picked]} if offered else None
            await self._end(entry, episode, by="remote", answers=answers)
        elif outcome is Outcome.ELSEWHERE:
            await self._end(entry, episode, by="terminal")
        elif outcome is Outcome.GONE:
            await self._end(entry, episode)
        elif outcome is Outcome.FAILED:
            await self._give_up(entry, episode)

    async def _give_up(self, entry: SessionEntry, episode: LimitEpisode) -> None:
        """The menu cannot be answered from here: the card expires, and the person is told."""
        if not self._current(entry, episode):
            return
        card, episode.card = episode.card, None
        if card is not None:
            await _emit(entry, card, status="expired")
        episode.mode = Mode.NOTICED
        await _say(entry, episode, FAILED)
        await self.control.limit_changed(entry)

    async def _end(
        self,
        entry: SessionEntry,
        episode: LimitEpisode,
        *,
        by: str | None = None,
        answers: dict[str, Any] | None = None,
    ) -> None:
        """The menu is gone: its card resolves `by` whoever answered it, or expires."""
        state = entry.shared
        if state is None or state.limit is not episode:
            return
        state.limit = None
        _stop(episode)
        card = episode.card
        if card is not None:
            fields: dict[str, Any] = {"by": by} if by is not None else {}
            if answers is not None:
                fields["answers"] = answers
            await _emit(entry, card, status="resolved" if by else "expired", **fields)
        await self.control.limit_changed(entry)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _current(entry: SessionEntry, episode: LimitEpisode) -> bool:
        state = entry.shared
        return state is not None and state.limit is episode

    def _recent(self, limit: LimitStop) -> bool:
        """A stop a transcript read long after it was written opens no menu (A63)."""
        return limit.at_ms is None or self.control.clock() - limit.at_ms <= RECENT_MS

    def _wake_at_reset(self, entry: SessionEntry, resets_at_ms: int | None) -> None:
        """Have the session looked at once what it holds may go, a minute after the reset."""
        reader = self.control.reader
        if reader is None or resets_at_ms is None:
            return
        until = resets_at_ms + RESET_LEAD_MS
        delay = (until - self.control.clock()) / 1000 + WAKE_MARGIN
        if delay > 0:
            reader.settle_soon(entry.session.session_id, delay)

    @staticmethod
    def _record(entry: SessionEntry, episode: LimitEpisode) -> bool | None:
        """What Claude Code's own record says of the menu: open, gone, or None for nothing.

        It says gone only after it said open during this menu: a Claude Code
        that names the menu otherwise, or writes no status, says nothing here.
        """
        state = entry.shared
        if state is None or state.attachment.pid <= 0:
            return None
        said = live.dialog_open(state.attachment.pid, entry.session.session_id)
        if said is True:
            episode.recorded = True
            return True
        return False if said is False and episode.recorded else None


def _offered(episode: LimitEpisode) -> dict[str, str]:
    """The card's options, id to label."""
    card = episode.card
    if card is None:
        return {}
    return {str(option["id"]): str(option["label"]) for option in card.question["options"]}


def _question(episode: LimitEpisode) -> dict[str, Any]:
    """The card's one question: the CLI's sentence, the free options in screen order, Close."""
    rows = episode.menu.free if episode.menu is not None else ()
    said = " ".join(episode.said.split()) or LIMIT_REACHED
    options: list[dict[str, str]] = [
        {"id": row.kind, "label": limit_menu.card_label(row)} for row in rows
    ]
    options.append({"id": CLOSE, "label": CLOSE_LABEL, "description": CLOSE_DESCRIPTION})
    return {
        "id": QUESTION_ID,
        "prompt": f"{said}\n\n{WAITING}",
        "options": options,
        "multi": False,
        "allow_text": True,
    }


def _row_saying(menu: Menu, kind: str, label: str) -> Row | None:
    """The row of this option whose words, whole and unbroken, are still `label`."""
    for row in menu.free:
        if row.kind == kind and limit_menu.whole(row) and limit_menu.card_label(row) == label:
            return row
    return None


def _wait_label(episode: LimitEpisode) -> str | None:
    """The words of the menu's "Wait here", when the screen showed them whole."""
    row = episode.menu.row(WAIT) if episode.menu is not None else None
    return limit_menu.card_label(row) if row is not None and limit_menu.whole(row) else None


async def _emit(entry: SessionEntry, card: Card, *, status: str, **fields: Any) -> None:
    await entry.channel.emit(
        "question",
        block_id=card.block_id,
        request_id=card.request_id,
        questions=[dict(card.question)],
        status=status,
        **fields,
    )


async def _say(entry: SessionEntry, episode: LimitEpisode, text: str) -> None:
    """A notice in the session, which then waits on the person: no key is pressed for them."""
    episode.asks = True
    episode.auto = False
    episode.opened_at = episode.looked_at = time.monotonic()
    await entry.channel.notice("warn", text)


def _tail(text: str) -> str:
    """The end of the screen as read, which the proxy checks is still its end before typing."""
    return text.rstrip()[-TAIL_CHARS:]


def _keyed_since(idle_for: float, since: float) -> bool:
    """Whether the person pressed a key after `since`, given how long the keyboard is quiet."""
    return time.monotonic() - idle_for > since + HUMAN_SLACK


def _gone(text: str, episode: LimitEpisode) -> bool:
    """Whether the screen shows the menu drawn over: what follows its last frame is foreign."""
    if episode.menu is None:
        return not limit_menu.shows_dialog(text)
    menu = limit_menu.find_menu(text)
    if menu is None:
        return not limit_menu.shows_dialog(text)
    return not menu.last


async def _screen(link: PtyLink) -> str | None:
    try:
        return (await link.screen()).text
    except RcError:
        return None


async def _steady(link: PtyLink) -> TerminalState | None:
    """The screen once it and the keyboard have both been still past Claude Code's guard."""
    for _ in range(STEADY_TRIES):
        first = await link.screen()
        await asyncio.sleep(SETTLE)
        second = await link.screen()
        if first.text == second.text and second.idle_for >= SETTLE:
            return second
    return None


def _stop(episode: LimitEpisode) -> None:
    """Stop following a menu that is over, unless this is the task that follows it."""
    task, episode.task = episode.task, None
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()
