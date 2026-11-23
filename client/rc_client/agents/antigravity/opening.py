"""How the device opens an Antigravity conversation it runs no process for (A64, 4.4).

A new session starts with `session/new`. One the device has every turn of — it created or last
continued the conversation, and `agy`'s list shows no write since — opens with `session/resume`,
which replays nothing; any other with `session/load`, which replays the whole conversation first.
`agy` would start an empty conversation under an id it no longer has, so a conversation missing
from its list is never loaded: the message is refused. Before anything opens, the device looks once
more for an `agy` it did not start that may have the conversation open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from . import errors, runtime
from .listing import Listed
from .replay import Ledger

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry
    from .service import AntigravityService

# How much later than the device's own last write `agy`'s list may date a conversation and still
# hold nothing the device has not seen: `agy` stamps a write in whole seconds, as it saves it.
RESUME_SLACK_MS = 5000

Kind = Literal["new", "resume", "load"]


@dataclass(frozen=True, slots=True)
class Opening:
    kind: Kind
    session_id: str | None = None
    listed: Listed | None = None


NEW = Opening("new")


def refuse_old(version: str | None) -> None:
    if version is not None and runtime.too_old(version):
        raise errors.too_old(version)


async def plan(service: AntigravityService, entry: SessionEntry, binary: str) -> Opening:
    """How to open the conversation of `entry`, or the refusal its message gets."""
    session_id = entry.session.session_id
    listed = (await service.fresh_list(binary)).get(session_id)
    await service.check_presence(entry, binary)
    if listed is None:
        raise errors.gone()
    wrote = Ledger.load(entry.channel.registry, session_id).wrote
    if wrote is not None and listed.updated_ms <= wrote + RESUME_SLACK_MS:
        return Opening("resume", session_id, listed)
    return Opening("load", session_id, listed)
