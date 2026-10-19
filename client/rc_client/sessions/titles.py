"""Which title a session shows, and who is allowed to change it.

Three sources, in order of precedence:

1. a title the user set through `session.create` or `session.set`, which is
   sticky and is never overwritten by anything an agent produces;
2. the agent's own summary of the conversation — Claude Code writes an
   `ai-title` row into its transcript, Codex names a thread — which replaces a
   first-prompt title whenever it arrives or changes;
3. the first line of the first message, until something better shows up. A
   first message of files and no words is named after the files, and the
   agent's summary waits until the person has said something: all it had to go
   on were the paths it was given (A49).

Whether the user pinned a title, and whether a session is named after its
files, is the device's own business, so both live in the registry's key/value
table rather than on the wire object.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ..attachments import without_paths
from ..models import title_from_message, title_from_text
from ..registry import Registry

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .channel import SessionChannel

PIN_PREFIX = "title-pinned:"
FILES_PREFIX = "title-files:"

# What a message carries about its files: the name, at least.
Files = Sequence[Mapping[str, Any]]


def _key(session_id: str) -> str:
    return f"{PIN_PREFIX}{session_id}"


def _files_key(session_id: str) -> str:
    return f"{FILES_PREFIX}{session_id}"


def pinned(registry: Registry, session_id: str) -> bool:
    """Whether the title of this session was chosen by the user."""
    return registry.get_kv(_key(session_id)) == "1"


def pin(registry: Registry, session_id: str) -> None:
    """Record that the user named this session, so no agent title replaces it."""
    registry.set_kv(_key(session_id), "1")


def named_after_files(registry: Registry, session_id: str) -> bool:
    """Whether the session is named after files nobody has said a word about yet."""
    return registry.get_kv(_files_key(session_id)) == "1"


def rekey(registry: Registry, old_id: str, new_id: str) -> None:
    """Carry a pin, and a name taken from files, across the move to the agent's own id."""
    if old_id == new_id:
        return
    if pinned(registry, old_id):
        pin(registry, new_id)
    if named_after_files(registry, old_id):
        registry.set_kv(_files_key(new_id), "1")


async def from_user(channel: SessionChannel, title: str) -> bool:
    """Apply a title the user typed, and make it stick."""
    pin(channel.registry, channel.session.session_id)
    return await _apply(channel, title)


async def from_agent(channel: SessionChannel, title: str) -> bool:
    """Apply the agent's own summary, unless the user has named the session.

    Nor while the session is named after files nobody has said anything about:
    the agent could only summarise the paths it was given (A49).
    """
    session_id = channel.session.session_id
    if pinned(channel.registry, session_id) or named_after_files(channel.registry, session_id):
        return False
    return await _apply(channel, title)


async def from_prompt(channel: SessionChannel, text: str, attachments: Files | None = None) -> bool:
    """Name a still-unnamed session after its first message.

    A slash command is published as a message since amendment A32 but names
    nothing, which is what `title_from_message` refuses. A message of files
    and no words is named after the files (A49), and the first words the
    person says let the agent's own summary in again.
    """
    session_id = channel.session.session_id
    said = title_from_message(text)
    if said and channel.session.title and named_after_files(channel.registry, session_id):
        channel.registry.set_kv(_files_key(session_id), "")
    if channel.session.title:
        return False
    if said or not attachments:
        return await _apply(channel, said)
    return await _from_files(channel, attachments)


async def _from_files(channel: SessionChannel, attachments: Files) -> bool:
    names = [str(item.get("name") or "").strip() for item in attachments]
    title = title_from_text(", ".join(name for name in names if name))
    if not title:
        return False
    channel.registry.set_kv(_files_key(channel.session.session_id), "1")
    return await _apply(channel, title)


async def _apply(channel: SessionChannel, text: str) -> bool:
    # An agent's summary may quote the prompt, attachment paths and all (A49).
    title = title_from_text(without_paths(text))
    if not title or title == channel.session.title:
        return False
    await channel.set_meta(title=title)
    return True
