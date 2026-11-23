"""Which sessions the device created and has held ever since (A64, 4.4 rule 3).

A session the device created with `session.create` and still holds is held by an `agy` naming it
on its command line alone, so the person's terminals in its directory leave it alone. The hold A58
gives a conversation a message continued — a hand-off from `agy`'s list, a message `agy`'s list or a
holder refused, a session continued after the person closed it — grants none of that: such a
conversation goes through every rule on every send. So the device records a creation when `agy`
names the new conversation, and forgets it for good once the person closes the session or the
session is found no longer held.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...registry import Registry

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry

KEY_PREFIX = "antigravity-created:"


def key(session_id: str) -> str:
    return f"{KEY_PREFIX}{session_id}"


def mark(registry: Registry, session_id: str) -> None:
    """The device has just created this conversation, and holds it."""
    registry.set_kv(key(session_id), "1")


def forget(registry: Registry, session_id: str) -> None:
    """The person closed the session, or the device no longer holds it: the exemption is gone."""
    if registry.get_kv(key(session_id)):
        registry.set_kv(key(session_id), "")


def exempt(entry: SessionEntry) -> bool:
    """Whether only an `agy` naming the conversation holds it: created here, held ever since."""
    session = entry.session
    if session.origin != "remote":
        return False
    registry = entry.channel.registry
    if not session.held or session.archived:
        forget(registry, session.session_id)
        return False
    return registry.get_kv(key(session.session_id)) == "1"
