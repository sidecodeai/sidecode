"""What a device update would throw away (A36, A56).

An update restarts the daemon. A session the device drives ends with it, and so does everything the
daemon holds in memory for a terminal session it is attached to: the turn it is reading, the
prompts it is relaying and the messages waiting for the CLI to be ready. The CLI itself is not the
daemon's and survives, but the person on the phone feels the restart, so a session in any of those
states counts as busy and the update waits for it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry

# A session in one of these states is doing work an update would throw away.
BUSY_STATES = frozenset({"starting", "running", "needs_approval", "needs_input"})


def busy_sessions(entries: Iterable[SessionEntry]) -> int:
    """How many of these sessions an update would interrupt."""
    return sum(1 for entry in entries if is_busy(entry))


def is_busy(entry: SessionEntry) -> bool:
    """Whether an update would interrupt this session.

    Only a session something holds can be: one the device drives, or one a terminal CLI shares
    with it. Messages queued for a session nothing holds wait for the next send to resume it,
    and they would wait for that however long the update did. A held session is busy while it
    works or waits on a person, while messages are queued for it, and, for a terminal session,
    while its CLI has a turn open or the device holds a prompt or a steered message for it.
    """
    shared = entry.shared
    if entry.runner is None and shared is None:
        return False
    if entry.queue or entry.session.state in BUSY_STATES:
        return True
    return shared is not None and bool(
        entry.session.turn is not None
        or shared.approvals
        or shared.question is not None
        or shared.steers
    )
