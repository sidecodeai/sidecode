"""Amendment A55: a session's name is the agent's.

`session.set` with a `title` renames the session inside its agent, as `/rename`
(Claude Code, Codex, Grok Build) or `/name` (pi) would, so a terminal that opens
or resumes the session shows the same name. The device is the authority on what
a name may be and on what the agent now holds it as:

- `normalise` makes a name safe to type, write and send, or refuses it;
- `apply` hands it to the agent's plugin (`registry.rename_in_agent`) under the
  session's lock, then follows what the agent says the session is called.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any

from ..agents.registry import RenameSpec, rename_in_agent
from ..errors import RcError
from ..models import MAX_TITLE
from . import titles

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry, SessionHub

# Control, format, line and paragraph separator characters become spaces. Lone
# surrogates join them: JSON can carry one, and UTF-8, which every route a name
# takes ends in, cannot.
_SPACED = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# Two format characters stay: the joiners that hold an emoji sequence (a woman
# technologist is a woman, a joiner and a laptop) and the words of Persian and of
# the Indic scripts together. A name of nothing but them is still no name.
_JOINERS = frozenset({"\u200c", "\u200d"})


def normalise(title: object) -> str:
    """The name a request asks for, or the `bad_request` that refuses it.

    Runs of white space collapse to one space and the ends are trimmed. There is
    no NFKC rewrite, so CJK, emoji and full-width forms stay as typed, and the
    zero-width joiner and non-joiner are kept. The limit counts code points,
    which is what every app counts.
    """
    if not isinstance(title, str):
        raise RcError("bad_request", "title must be a string")
    spaced = "".join(
        " " if unicodedata.category(char) in _SPACED and char not in _JOINERS else char
        for char in title
    )
    name = " ".join(spaced.split())
    if not any(char not in _JOINERS and not char.isspace() for char in name):
        raise RcError("bad_request", "title must not be empty")
    if len(name) > MAX_TITLE:
        raise RcError("bad_request", f"title is too long (max {MAX_TITLE} characters)")
    return name


def requested(params: dict[str, Any]) -> str | None:
    """The normalised name a `session.set` frame asks for; `None` when it asks for none."""
    title = params.get("title")
    return None if title is None else normalise(title)


def spec_for(hub: SessionHub, entry: SessionEntry, name: str) -> RenameSpec:
    """What the plugin's hook is given: the session, and every service it may need."""
    return RenameSpec(
        entry=entry,
        info=hub.agent_info(entry.session.agent),
        name=name,
        shared=hub.shared,
        codex_daemon=hub.codex_daemon,
        grok_leader=hub.grok_leader,
        pi_extensions=hub.pi_extensions,
    )


def _held(entry: SessionEntry, name: str) -> bool:
    """Whether the session already carries `name` as the user's own choice."""
    session = entry.session
    return session.title == name and titles.pinned(entry.channel.registry, session.session_id)


def _still_there(hub: SessionHub, entry: SessionEntry) -> None:
    """Refuse to go on for a session that was deleted: `_remove` does not take the lock.

    Pinning and publishing a title for it would bring it back, in the registry
    and in every app.
    """
    session_id = entry.session.session_id
    if hub.entries.get(session_id) is not entry:
        raise RcError("not_found", f"unknown session {session_id}")


async def apply(hub: SessionHub, entry: SessionEntry, name: str) -> None:
    """Rename the session inside its agent, then take on the name the agent holds.

    A name the session already has is answered at once. Otherwise the plugin
    renames it under `entry.lock`, which keeps a message from being injected
    between the keystrokes of a typed rename; a second request for the same name
    waits there and finds it taken when its turn comes, instead of typing it
    again. Nothing is pinned or published until the agent has taken the name, so
    a refusal leaves the title as it was, and neither is a session that was
    deleted while it waited or while the agent worked: that is `not_found`. The
    title that follows is the agent's answer, which can differ from the request,
    and it never moves the session up the list. A hook that answers nothing is
    taken to hold the name it was given.
    """
    if _held(entry, name):
        return
    spec = spec_for(hub, entry, name)
    async with entry.lock:
        if _held(entry, name):
            return
        _still_there(hub, entry)
        try:
            held = await rename_in_agent(entry.session.agent, spec)
        except Exception:
            # A session deleted meanwhile is not found, whatever the agent said.
            _still_there(hub, entry)
            raise
        _still_there(hub, entry)
        await titles.from_user(entry.channel, held or name)
