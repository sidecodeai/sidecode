"""Amendment A47: the mark on a session that stopped working and waits for the person (§4.4).

A session works while its state is `running`, a turn under way, and waits for the person while it
asks for an approval or an answer, or rests `idle` or `readonly` with something still holding it:
the status dot's amber. A device moving a session from the first to the second marks it. `starting`
is green too, but a session that only started has done nothing to look at, so it neither brings the
mark nor counts as working again. The transition is read from the states alone, so a turn that
ended while the device was offline marks the session when the device reports it. The session working
again or being archived takes the mark away, and so does an app's `session.seen`, which the hub
applies itself. Nothing else touches it: a session that errored, or whose CLI exited after its
turn, keeps the mark it had and never earns one.

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


def is_working(state: str) -> bool:
    return state == WORKING_STATE


def is_waiting(state: str, control: str) -> bool:
    return state in ASKING_STATES or (state in RESTING_STATES and control != "none")


def next_unseen(
    previous_state: str | None, previously_unseen: bool, summary: Mapping[str, Any]
) -> bool:
    """The mark a session carries once ``summary`` replaces the one stored for it.

    ``previous_state`` is ``None`` for a session the gateway has never seen: there is no transition
    to read, so it starts unmarked.
    """
    state = _text(summary, "state")
    if summary.get("archived") or is_working(state):
        return False
    if (
        previous_state is not None
        and is_working(previous_state)
        and is_waiting(state, _text(summary, "control"))
    ):
        return True
    return previously_unseen


def with_mark(summary: dict[str, Any], unseen: bool) -> dict[str, Any]:
    """``summary`` as an app receives it: the gateway's mark when it is set, never a device's.

    Absent is false (§4.4), so a cleared mark is no field at all. The summary is changed in place
    and returned, so callers hand it a copy they own.
    """
    summary.pop(FIELD, None)
    if unseen:
        summary[FIELD] = True
    return summary


def _text(summary: Mapping[str, Any], name: str) -> str:
    value = summary.get(name)
    return value if isinstance(value, str) else ""
