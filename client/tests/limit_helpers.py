"""Claude Code's usage-limit menu as a terminal shows it, for the A63 tests.

The rows are Claude Code's own wordings, from 2.1.318 to 2.1.326.
`menu_text` is what the pseudo-terminal proxy keeps of a menu drawn in
full: Claude Code moves down a line with a carriage return and a cursor move,
which the proxy's screen reader turns into an empty line between rows.
`LimitTerminal` is a terminal whose CLI holds that menu and takes keys the way
it does: a number picks its row without Enter, Escape cancels, a key within its
guard after a draw or a key is ignored, and what it draws next lands after what
it drew before, as the proxy keeps it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from rc_client.agents.claude import live
from rc_client.sessions import limit_dialog
from rc_client.sessions.ptys import PtyLink, TerminalState

TITLE = "What do you want to do?"
FOOTER = "Enter to confirm \u00b7 Esc to cancel"
HIGHLIGHT = "\u276f"
SAID = "You've hit your 5-hour limit \u00b7 resets 6:30pm (UTC)"

STOP_WAIT = "Stop and wait for limit to reset"
WAIT_AT = "Wait here, then continue automatically at 6:30pm"
WAIT_SHORTLY = "Wait here, then continue automatically shortly"
WAIT_RESET = "Wait here, then continue automatically when the limit resets"
CREDITS = "Switch to usage credits"
STOP = "Stop"
NO_AUTO = "Don't continue automatically"
UPGRADE = "Upgrade your plan"
FUNDS = "Add funds to keep going\u2026"
ADMIN = "Ask your admin for more usage\u2026"

# What the CLI draws where the menu was once it closes: its prompt box and hints.
PROMPT_BOX = (
    "\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
    f"{HIGHLIGHT}\u00a0\n\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n"
    "  ? for shortcuts\n"
)
# What the terminal shows above the menu: the end of the conversation.
CONVERSATION = "\u23bf  " + SAID + "\n\n"


def menu_text(
    rows: Sequence[str],
    *,
    current: int = 1,
    title: str = TITLE,
    footer: str = FOOTER,
) -> str:
    """A menu drawn in full, as the proxy's screen text has it."""
    lines = [f" {title}", ""]
    for number, label in enumerate(rows, start=1):
        marker = HIGHLIGHT if number == current else " "
        lines.append(f" {marker} {number}. {label}")
    lines += ["", f" {footer}"]
    return "\n\n".join(lines) + "\n"


class _NoSocket:
    """Stands in for the stream a real link writes its frames on."""

    def close(self) -> None:
        return None


class LimitTerminal(PtyLink):
    """A terminal whose Claude Code holds its usage-limit menu, and takes keys as it does.

    `swallow` is how many keys the menu ignores, as its guard ignores one that
    comes within 250 ms of a draw or a key; such a key draws nothing. A
    terminal that `shows` nothing is one whose CLI went on waiting by itself.
    Keys sent on the condition that the screen still ends as it was read are
    held back, as the proxy holds them, when it does not; `drawn_before_key` is
    something the CLI draws in the moment between the read and the key.
    """

    def __init__(
        self,
        rows: Sequence[str] = (STOP_WAIT, WAIT_AT, CREDITS),
        *,
        swallow: int = 0,
        shows: bool = True,
    ) -> None:
        super().__init__(4242, cast(asyncio.StreamWriter, _NoSocket()))
        self.rows = list(rows)
        self.open = shows
        self.text = CONVERSATION + (menu_text(self.rows) if shows else PROMPT_BOX)
        self.typed: list[str] = []
        self.chosen: str | None = None
        self.swallow = swallow
        self.last_key_at = time.monotonic() - 60.0
        self.drawn_before_key = ""
        self.withheld: list[str] = []

    @property
    def idle_for(self) -> float:
        return max(0.0, time.monotonic() - self.last_key_at)

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        if clear:
            self.text = ""
        self.typed.append(data)
        self._press(data)
        return TerminalState(0, self.idle_for)

    async def keys_if_shown(self, data: str, tail: str, clear: bool = False) -> TerminalState:
        self.text += self.drawn_before_key
        self.drawn_before_key = ""
        if not self.text.rstrip().endswith(tail.rstrip()):
            self.withheld.append(data)
            return TerminalState(0, self.idle_for, sent=False)
        return await self.keys(data, clear)

    async def screen(self) -> TerminalState:
        return TerminalState(0, self.idle_for, self.text)

    async def state(self) -> TerminalState:
        return TerminalState(0, self.idle_for)

    def _press(self, data: str) -> None:
        if not self.open:
            # A key past the menu lands in the prompt box.
            self.text += data
            return
        if self.swallow > 0:
            self.swallow -= 1
            return
        if data == limit_dialog.ESCAPE:
            self.close("escape")
        elif data.isdigit() and 1 <= int(data) <= len(self.rows):
            self.close(self.rows[int(data) - 1])

    def close(self, chosen: str) -> None:
        self.open = False
        self.chosen = chosen
        self.text += PROMPT_BOX

    def person_answers(self, number: int) -> None:
        """The person presses a row's number in the terminal themselves."""
        self.last_key_at = time.monotonic()
        self.close(self.rows[number - 1])

    def person_types(self) -> None:
        """The person presses a key in the terminal that the menu does nothing with."""
        self.last_key_at = time.monotonic()

    def redraw(self, rows: Sequence[str]) -> None:
        """The CLI draws its menu again, with these rows: a time that moved, an option gone."""
        self.rows = list(rows)
        self.text += menu_text(self.rows)

    def person_moves(self) -> None:
        """The person presses Down: the highlight moves, and the menu stays."""
        self.last_key_at = time.monotonic()
        self.text += f"\n\n   1. {self.rows[0]}\n\n {HIGHLIGHT} 2. {self.rows[1]}\n"


def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every wait of `limit_dialog` made short, so a test runs in a moment."""
    for name, value in {
        "DETECT_WINDOW": 0.3,
        "DETECT_POLL": 0.01,
        "WATCH_POLL": 0.02,
        "QUIET": 0.05,
        "SETTLE": 0.02,
        "STEADY_TRIES": 3,
        "CONFIRM_WAIT": 0.15,
        "CONFIRM_POLL": 0.01,
        "HUMAN_SLACK": 0.01,
    }.items():
        monkeypatch.setattr(limit_dialog, name, value)


def write_record(pid: int, session_id: str, **fields: Any) -> Path:
    """Claude Code's own record of a CLI, as `live.py` reads it."""
    import json

    directory = live.sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "sessionId": session_id,
        "procStart": "Tue Nov 17 09:00:00 2026",
        "kind": "interactive",
        "entrypoint": "cli",
        **fields,
    }
    path = directory / f"{pid}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def until(ready: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait for the device to get somewhere, or fail saying it never did."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the device never got that far")


class Clock:
    """The clock the reset is read on, moved by hand."""

    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now
