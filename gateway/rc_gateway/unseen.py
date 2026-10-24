"""Amendment A47: the mark on a session that stopped working and waits for the person (§4.4).

A session works while its state is `running`, a turn under way, and waits for the person while it
asks for an approval or an answer, or rests `idle` or `readonly` with something still holding it:
the status dot's amber. A device moving a session from the first to the second marks it. `starting`
is green too, but a session that only started has done nothing to look at, so it neither brings the
mark nor counts as working again. The transition is read from the states alone, so a turn that
ended while the device was offline marks the session when the device reports it. A session that
errored keeps the mark it had and never earns one.

The gateway keeps the mark and shows it, and the two differ. Kept, it is the fact that nobody has
looked since the session stopped working, and only three things take it away for good: the session
working again, an app's `session.seen`, which the hub applies itself, and an explicit close, a
session archived (A39's close ends in `archived: true`, `control: none` and `state: stopped`
together) or stopped. Shown, it is what apps draw and counts add up, and it is the kept mark of a
session that is neither closed nor unheld. `control: none` is no close. It is what a device says of
a CLI that exited, and also of a session whose terminal it has not found yet, a pi extension that
has not reconnected and a scan that missed its holder, and it comes and goes. Every app files an
unheld session in its Archive, so the mark is kept and not shown while nothing holds the session,
and shown again when something does.

The mark is the gateway's alone. Whatever a device puts in a summary under that name is dropped,
and the stored summaries never hold it: the index keeps it in a column of its own and lays it over
each summary as an app receives it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FIELD = "unseen"
#: A running turn, and nothing else, is the work a mark follows (A47).
WORKING_STATE = "running"
#: Waiting whoever holds the session: it has asked the person something.
ASKING_STATES = frozenset({"needs_approval", "needs_input"})
#: Waiting only while something holds the session; with `control: none` nobody is left to answer.
RESTING_STATES = frozenset({"idle", "readonly"})
#: The agent's process ended: the state A39's close leaves a session in.
STOPPED_STATE = "stopped"
#: Nothing holds the session: no process, no device (§4.4).
UNHELD_CONTROL = "none"


def is_working(state: str) -> bool:
    return state == WORKING_STATE


def is_waiting(state: str, control: str) -> bool:
    return state in ASKING_STATES or (state in RESTING_STATES and control != UNHELD_CONTROL)


def is_closed(summary: Mapping[str, Any]) -> bool:
    """Whether ``summary`` is an explicit close, which takes the mark away for good."""
    return bool(summary.get("archived")) or _text(summary, "state") == STOPPED_STATE


def is_unheld(summary: Mapping[str, Any]) -> bool:
    """Whether nothing holds the session, as far as ``summary`` says: no mark is shown for it."""
    return _text(summary, "control") == UNHELD_CONTROL


def shows(kept: bool, summary: Mapping[str, Any]) -> bool:
    """Whether apps are shown a session's mark: it is kept, and the session is open and held."""
    return kept and not is_closed(summary) and not is_unheld(summary)


def next_unseen(
    previous_state: str | None, previously_unseen: bool, summary: Mapping[str, Any]
) -> bool:
    """The mark kept for a session once ``summary`` replaces the one stored for it.

    ``previous_state`` is ``None`` for a session the gateway has never seen: there is no transition
    to read, so it starts unmarked.
    """
    state = _text(summary, "state")
    if is_closed(summary) or is_working(state):
        return False
    if (
        previous_state is not None
        and is_working(previous_state)
        and is_waiting(state, _text(summary, "control"))
    ):
        return True
    return previously_unseen


def with_mark(summary: dict[str, Any], kept: bool) -> dict[str, Any]:
    """``summary`` as an app receives it: the mark when it is shown, never a device's.

    Absent is false (§4.4), so a mark that is not shown is no field at all. The summary is changed
    in place and returned, so callers hand it a copy they own.
    """
    summary.pop(FIELD, None)
    if shows(kept, summary):
        summary[FIELD] = True
    return summary


def _text(summary: Mapping[str, Any], name: str) -> str:
    value = summary.get(name)
    return value if isinstance(value, str) else ""
