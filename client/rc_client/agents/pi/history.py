"""Replay the branch a terminal pi session already had, as protocol events.

A `hello` carries `ctx.sessionManager.getBranch()`, which is every entry on the
path the session is currently on. The device publishes it once, when it first
meets the session, so an app opening a conversation somebody started an hour ago
in a terminal reads it from the beginning rather than from the next word.

Timestamps come from the entries themselves, so the transcript keeps the shape
of the afternoon it happened in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..base import Emit
from .translate import MAX_OUTPUT_CHARS, text_of, tool_kind, tool_title

# Entry kinds that carry a message; everything else in a branch is a marker.
MESSAGE = "message"


def _millis(entry: dict[str, Any], message: dict[str, Any]) -> int | None:
    """The entry's own moment: pi writes ISO on the entry and epoch ms inside."""
    stamp = message.get("timestamp")
    if isinstance(stamp, int) and stamp > 0:
        return stamp
    raw = str(entry.get("timestamp") or "")
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _stamped(kind: str, ts: int | None, fields: dict[str, Any]) -> Emit:
    return Emit(kind, {**fields, **({"ts": ts} if ts else {})})


def _user(entry_id: str, message: dict[str, Any], ts: int | None) -> list[Emit]:
    text = text_of(message.get("content"))
    if not text.strip():
        return []
    return [
        _stamped(
            "user_message",
            ts,
            {"block_id": f"h:{entry_id}", "text": text, "source": "terminal"},
        )
    ]


def _assistant(
    entry_id: str, message: dict[str, Any], ts: int | None, calls: dict[str, dict[str, Any]]
) -> list[Emit]:
    emits: list[Emit] = []
    content = message.get("content")
    for index, item in enumerate(content if isinstance(content, list) else []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        block = f"h:{entry_id}:{index}"
        if kind == "thinking":
            emits.append(_block(block, "thinking", str(item.get("thinking") or ""), ts))
        elif kind == "text":
            emits.append(_block(block, "assistant_text", str(item.get("text") or ""), ts))
        elif kind == "toolCall":
            call_id = str(item.get("id") or "")
            if call_id:
                calls[call_id] = {"item": item, "ts": ts}
    return [emit for emit in emits if emit.fields.get("text")]


def _block(block_id: str, kind: str, text: str, ts: int | None) -> Emit:
    return _stamped(kind, ts, {"block_id": block_id, "text": text, "done": True})


def _tool_result(
    message: dict[str, Any], ts: int | None, calls: dict[str, dict[str, Any]]
) -> list[Emit]:
    call_id = str(message.get("toolCallId") or "")
    started = calls.pop(call_id, None)
    item = started["item"] if started else {}
    name = str(message.get("toolName") or item.get("name") or "tool")
    arguments = item.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    started_at = started["ts"] if started else ts
    fields: dict[str, Any] = {
        "block_id": call_id or f"h:{id(message)}",
        "tool": name,
        "tool_kind": tool_kind(name),
        "title": tool_title(arguments, name),
        "status": "failed" if message.get("isError") else "succeeded",
        "started_at": started_at or 0,
    }
    if arguments:
        fields["input"] = arguments
    output = text_of(message.get("content"))[:MAX_OUTPUT_CHARS]
    if output:
        fields["output"] = output
    if ts:
        fields["ended_at"] = ts
        fields["duration_ms"] = max(0, ts - (started_at or ts))
    return [_stamped("tool_call", ts, fields)]


def replay(entries: list[Any]) -> list[Emit]:
    """Every block a branch already holds, oldest first."""
    emits: list[Emit] = []
    calls: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != MESSAGE:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        entry_id = str(entry.get("id") or len(emits))
        ts = _millis(entry, message)
        role = str(message.get("role") or "")
        if role == "user":
            emits.extend(_user(entry_id, message, ts))
        elif role == "assistant":
            emits.extend(_assistant(entry_id, message, ts, calls))
        elif role == "toolResult":
            emits.extend(_tool_result(message, ts, calls))
    return emits
