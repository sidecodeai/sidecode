"""The subagents a Codex thread spawned, and whether any of them is working.

Codex runs a subagent in a thread of its own, announced like every other thread
and naming its parent. Such a thread is never a session here (A18), but a
parent is working while any child of its own is, so the device follows the
children: their own turns when the daemon sends them, and the two item shapes a
parent's timeline gives for them when it does not.
"""

from __future__ import annotations

import time
from typing import Any

from ..translate import item_type

# What an `agentsStates` entry says about a child that has stopped working.
# Everything else — `in_progress`, and any word a later Codex adds — is work.
FINISHED = frozenset(
    {"completed", "failed", "declined", "cancelled", "closed", "interrupted", "aborted"}
)
# A child that has said nothing for this long is taken as gone, so a subagent
# killed mid-turn cannot hold its parent's turn open for the rest of the day.
CHILD_STALE_S = 1800.0
# Children remembered per device, so a notification for one reaches its parent.
# Every one of them is a subagent of a session on this machine.
CHILD_THREADS = 256


def child_states(item: dict[str, Any], completed: bool) -> dict[str, bool]:
    """`child thread id -> still working`, read from one normalised thread item.

    Two item shapes name a subagent. The tool call the parent made carries
    `agentsStates`, which is the daemon's own answer for every agent it holds
    and therefore outranks everything else; the threads it addresses without a
    state of their own are working unless the call was the one that closes
    them. The activity row a subagent's work produces names one thread, and
    that work is under way until the row itself completes.
    """
    kind = item_type(item)
    if kind == "subAgentActivity":
        child = _text(item.get("agentThreadId"))
        return {child: not completed} if child else {}
    if kind != "collabAgentToolCall":
        return {}
    found: dict[str, bool] = {}
    closing = str(item.get("tool") or "") == "close_agent"
    for child in _receivers(item.get("receiverThreadIds")):
        found[child] = not closing
    found.update(_states(item.get("agentsStates")))
    return found


def _states(value: Any) -> dict[str, bool]:
    """Read `agentsStates` as a map or as the list of entries it may also be."""
    entries: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        entries = [(str(key), state) for key, state in value.items()]
    elif isinstance(value, list):
        entries = [(_child_of(entry), entry) for entry in value if isinstance(entry, dict)]
    found: dict[str, bool] = {}
    for child, state in entries:
        if child:
            found[child] = _word(state) not in FINISHED
    return found


def _child_of(entry: dict[str, Any]) -> str:
    agent = entry.get("agent")
    if isinstance(agent, dict):
        return _text(agent.get("threadId")) or _text(agent.get("thread_id"))
    return _text(entry.get("threadId")) or _text(entry.get("agentThreadId"))


def _word(state: Any) -> str:
    """The state itself, whether Codex sent the word or an object around it."""
    if isinstance(state, dict):
        for key in ("status", "state", "type"):
            found = _text(state.get(key))
            if found:
                return found.lower()
        return ""
    return _text(state).lower()


def _receivers(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class ChildWatch:
    """The children of one session that are still working."""

    def __init__(self, stale_after: float | None = None) -> None:
        self._stale_after = CHILD_STALE_S if stale_after is None else stale_after
        self._seen: dict[str, float] = {}

    @property
    def working(self) -> bool:
        """Whether any child is still at work, silence longer than the bound aside."""
        self._drop_stale()
        return bool(self._seen)

    @property
    def count(self) -> int:
        return len(self._seen)

    def note(self, child_id: str, active: bool) -> bool:
        """Record one child's state; True when that was the last one working."""
        if active:
            self._seen[child_id] = time.monotonic()
            return False
        was_working = bool(self._seen)
        self._seen.pop(child_id, None)
        self._drop_stale()
        return was_working and not self._seen

    def forget(self) -> None:
        self._seen.clear()

    def _drop_stale(self) -> None:
        cutoff = time.monotonic() - self._stale_after
        for child_id in [child for child, seen in self._seen.items() if seen < cutoff]:
            del self._seen[child_id]


class ChildIndex:
    """`child thread id -> the session that spawned it`, newest kept."""

    def __init__(self, limit: int = CHILD_THREADS) -> None:
        self._limit = limit
        self._parents: dict[str, str] = {}

    def remember(self, child_id: str, parent_id: str) -> None:
        self._parents.pop(child_id, None)
        self._parents[child_id] = parent_id
        while len(self._parents) > self._limit:
            self._parents.pop(next(iter(self._parents)))

    def parent(self, child_id: str) -> str | None:
        return self._parents.get(child_id)

    def forget(self, child_id: str) -> None:
        self._parents.pop(child_id, None)

    def __contains__(self, child_id: object) -> bool:
        return child_id in self._parents
