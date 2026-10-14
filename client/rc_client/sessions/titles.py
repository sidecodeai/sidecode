"""Which title a session shows, and who is allowed to change it.

Three sources, in order of precedence:

1. a title the user set through `session.create` or `session.set`, which is
   sticky and is never overwritten by anything an agent produces;
2. the agent's own summary of the conversation — Claude Code writes an
   `ai-title` row into its transcript, Codex names a thread — which replaces a
   first-prompt title whenever it arrives or changes;
3. the first line of the first message, until something better shows up.

Whether the user pinned a title is the device's own business, so it lives in
the registry's key/value table rather than on the wire object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import title_from_message, title_from_text
from ..registry import Registry

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .channel import SessionChannel

PIN_PREFIX = "title-pinned:"


def _key(session_id: str) -> str:
    return f"{PIN_PREFIX}{session_id}"


def pinned(registry: Registry, session_id: str) -> bool:
    """Whether the title of this session was chosen by the user."""
    return registry.get_kv(_key(session_id)) == "1"


def pin(registry: Registry, session_id: str) -> None:
    """Record that the user named this session, so no agent title replaces it."""
    registry.set_kv(_key(session_id), "1")


def rekey(registry: Registry, old_id: str, new_id: str) -> None:
    """Carry a pin across the move from a provisional id to the agent's own."""
    if old_id != new_id and pinned(registry, old_id):
        pin(registry, new_id)


async def from_user(channel: SessionChannel, title: str) -> bool:
    """Apply a title the user typed, and make it stick."""
    pin(channel.registry, channel.session.session_id)
    return await _apply(channel, title)


async def from_agent(channel: SessionChannel, title: str) -> bool:
    """Apply the agent's own summary, unless the user has named the session."""
    if pinned(channel.registry, channel.session.session_id):
        return False
    return await _apply(channel, title)


async def from_prompt(channel: SessionChannel, text: str) -> bool:
    """Name a still-unnamed session after its first message.

    A slash command is published as a message since amendment A32 but names
    nothing, which is what `title_from_message` refuses.
    """
    if channel.session.title:
        return False
    return await _apply(channel, title_from_message(text))


async def _apply(channel: SessionChannel, text: str) -> bool:
    title = title_from_text(text)
    if not title or title == channel.session.title:
        return False
    await channel.set_meta(title=title)
    return True
