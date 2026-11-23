"""Putting what `agy` said on a session's channel: streamed events, and rows a replay brought."""

from __future__ import annotations

import uuid
from typing import Any

from ...models import now_ms
from ...sessions.channel import SessionChannel
from ..base import Emit
from .replay import Row
from .translate import new_tool


async def emit(channel: SessionChannel, event: Emit) -> None:
    """One event; a streamed append is coalesced with the rest of its block."""
    if event.delta:
        fields = dict(event.fields)
        block_id = str(fields.pop("block_id"))
        delta = str(fields.pop("delta", ""))
        await channel.emit_delta(event.kind, block_id, delta, **fields)
        return
    await channel.emit(event.kind, **event.fields)


async def row(channel: SessionChannel, replayed: Row) -> None:
    """A replayed row the device had not published, timed when it was read (A64)."""
    if replayed.kind == "user":
        fields: dict[str, Any] = {
            "block_id": f"user:{uuid.uuid4()}",
            "text": replayed.text,
            "source": "terminal",
        }
        if replayed.attachments:
            fields["attachments"] = replayed.attachments
        await channel.emit("user_message", **fields)
    elif replayed.kind == "agent" and replayed.text:
        block_id = f"{uuid.uuid4().hex[:12]}:replay:assistant_text"
        await channel.emit("assistant_text", block_id=block_id, text=replayed.text, done=True)
    elif replayed.kind == "tool":
        await emit(channel, new_tool(replayed.update).emit(now_ms()))
