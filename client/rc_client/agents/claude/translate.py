"""Translate `claude-agent-sdk` messages into protocol session events.

Pure and stateful only in the way the timeline is: it remembers which block a
tool result belongs to, which message id the current stream deltas carry, and
where in its message each streamed text or thinking block sits.

That last part is what keeps one answer one block. The deltas name a block by
its index in the API message, but the CLI hands over a finished message one
block at a time, each in a message of its own, so a text block that follows a
thinking block arrives alone at position 0 while its deltas said 1. Read as
two blocks, the answer showed twice, once finished and once still streaming.
A finished block therefore takes the index its block started under in the
stream, and where nothing streamed, the count of the blocks earlier pieces of
its message carried.
"""

from __future__ import annotations

import uuid
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ...attachments import without_paths
from ...diffs import from_tool_input
from ...models import now_ms
from ...sessions.limits import claude_result_limit
from ..base import COMPACTION_NOTICE, Emit
from . import markers
from .injected import classify
from .tools import todos_from_input, tool_kind, tool_title

MAX_TOOL_BLOCKS = 2000
# How many messages keep the positions of their blocks until the finished
# blocks arrive; one that never finishes is let go of eventually.
MAX_POSITIONED_MESSAGES = 64
# The streamed block types a finished block is matched back to by position.
_POSITIONED = ("text", "thinking")


def result_text(content: Any) -> str:
    """Flatten a tool result payload into displayable text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)


class ClaudeTranslator:
    """One translator per live session."""

    def __init__(self, cwd: str | None = None) -> None:
        self.cwd = cwd
        self.session_id: str | None = None
        self.model: str | None = None
        self._stream_message_id: str | None = None
        # Per streamed message, per block type, the indices its blocks started
        # at and no finished block has claimed yet, in order.
        self._streamed: dict[str, dict[str, list[int]]] = {}
        # Per message, how many blocks the finished pieces of it carried so far.
        self._handed: dict[str, int] = {}
        self._tools: dict[str, dict[str, Any]] = {}
        self._interrupted = False

    def mark_interrupted(self) -> None:
        self._interrupted = True

    def clear_interrupt(self) -> None:
        self._interrupted = False

    # ------------------------------------------------------------- dispatch

    def feed(self, message: Any) -> list[Emit]:
        if isinstance(message, StreamEvent):
            return self._feed_stream(message)
        if isinstance(message, AssistantMessage):
            return self._feed_assistant(message)
        if isinstance(message, UserMessage):
            return self._feed_user(message)
        if isinstance(message, SystemMessage):
            return self._feed_system(message)
        if isinstance(message, ResultMessage):
            return self._feed_result(message)
        return self._feed_task(message)

    # --------------------------------------------------------------- pieces

    def _block_id(self, message_id: str | None, index: int) -> str:
        base = message_id or self._stream_message_id or "msg"
        return f"{base}:{index}"

    def _note_block(self, event: dict[str, Any]) -> None:
        """Remember where a streamed text or thinking block sits in its message."""
        block = event.get("content_block") or {}
        kind = block.get("type") if isinstance(block, dict) else None
        message_id = self._stream_message_id
        if message_id is None or kind not in _POSITIONED:
            return
        positions = self._streamed.setdefault(message_id, {})
        positions.setdefault(str(kind), []).append(int(event.get("index") or 0))
        for stale in list(self._streamed)[:-MAX_POSITIONED_MESSAGES]:
            self._streamed.pop(stale, None)

    def _handed_over(self, message: AssistantMessage) -> int:
        """How many blocks of this message its earlier finished pieces carried."""
        message_id = message.message_id
        if not message_id:
            return 0
        offset = self._handed.get(message_id, 0)
        self._handed[message_id] = offset + len(message.content)
        for stale in list(self._handed)[:-MAX_POSITIONED_MESSAGES]:
            self._handed.pop(stale, None)
        return offset

    def _finished_index(self, message_id: str | None, kind: str, position: int) -> int:
        """The index a finished block was streamed under, else its place in the message."""
        positions = self._streamed.get(message_id or "", {})
        started = positions.get(kind)
        if not started:
            return position
        index = started.pop(0)
        if not any(positions.values()):
            self._streamed.pop(message_id or "", None)
        return index

    def _feed_stream(self, message: StreamEvent) -> list[Emit]:
        event = message.event or {}
        event_type = event.get("type")
        if event_type == "message_start":
            raw = event.get("message") or {}
            self._stream_message_id = str(raw.get("id") or "") or None
            return []
        if event_type == "message_stop":
            self._stream_message_id = None
            return []
        if event_type == "content_block_start":
            self._note_block(event)
            return []
        if event_type != "content_block_delta":
            return []
        delta = event.get("delta") or {}
        index = int(event.get("index") or 0)
        fields: dict[str, Any] = {"block_id": self._block_id(None, index)}
        if message.parent_tool_use_id:
            fields["parent_block_id"] = message.parent_tool_use_id
        if delta.get("type") == "text_delta":
            fields["delta"] = str(delta.get("text") or "")
            return [Emit("assistant_text", fields, delta=True)] if fields["delta"] else []
        if delta.get("type") == "thinking_delta":
            fields["delta"] = str(delta.get("thinking") or "")
            return [Emit("thinking", fields, delta=True)] if fields["delta"] else []
        return []

    def _feed_assistant(self, message: AssistantMessage) -> list[Emit]:
        emits: list[Emit] = []
        parent = message.parent_tool_use_id
        for position, block in enumerate(message.content, start=self._handed_over(message)):
            if isinstance(block, TextBlock):
                emits.extend(self._finished(message, "text", position, block.text))
            elif isinstance(block, ThinkingBlock):
                emits.extend(self._finished(message, "thinking", position, block.thinking))
            elif isinstance(block, ToolUseBlock):
                emits.extend(self._tool_use(block, parent))
            elif isinstance(block, ToolResultBlock):
                emits.extend(self._tool_result(block, parent))
        return emits

    def _finished(
        self, message: AssistantMessage, kind: str, position: int, text: str
    ) -> list[Emit]:
        """A finished text or thinking block, under the id its deltas streamed into.

        The index is claimed even for an empty block, which publishes nothing,
        so the next finished block of the message still lands on its own.
        """
        index = self._finished_index(message.message_id, kind, position)
        if not text:
            return []
        fields: dict[str, Any] = {
            "block_id": self._block_id(message.message_id, index),
            "text": text,
            "done": True,
        }
        if message.parent_tool_use_id:
            fields["parent_block_id"] = message.parent_tool_use_id
        return [Emit("assistant_text" if kind == "text" else "thinking", fields)]

    def _tool_use(self, block: ToolUseBlock, parent: str | None) -> list[Emit]:
        tool_input = block.input if isinstance(block.input, dict) else {}
        started_at = now_ms()
        record = {
            "tool": block.name,
            "input": tool_input,
            "started_at": started_at,
            "parent": parent,
        }
        if len(self._tools) >= MAX_TOOL_BLOCKS:
            self._tools.pop(next(iter(self._tools)))
        self._tools[block.id] = record
        if block.name == "TodoWrite":
            items = todos_from_input(tool_input)
            return [Emit("todos", {"items": items})] if items else []
        fields: dict[str, Any] = {
            "block_id": block.id,
            "tool": block.name,
            "tool_kind": tool_kind(block.name),
            "title": tool_title(block.name, tool_input, self.cwd),
            "status": "running",
            "input": tool_input,
            "started_at": started_at,
        }
        if parent:
            fields["parent_block_id"] = parent
        return [Emit("tool_call", fields)]

    def _tool_result(self, block: ToolResultBlock, parent: str | None) -> list[Emit]:
        record = self._tools.get(block.tool_use_id)
        tool = str(record["tool"]) if record else "Tool"
        if tool == "TodoWrite":
            return []
        tool_input = record["input"] if record else {}
        started_at = int(record["started_at"]) if record else now_ms()
        ended_at = now_ms()
        failed = bool(block.is_error)
        fields: dict[str, Any] = {
            "block_id": block.tool_use_id,
            "tool": tool,
            "tool_kind": tool_kind(tool),
            "title": tool_title(tool, tool_input, self.cwd),
            "status": "failed" if failed else "succeeded",
            "input": tool_input,
            "output": result_text(block.content),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": max(0, ended_at - started_at),
        }
        if record is not None:
            record["finished"] = True
        parent_block = parent or (record.get("parent") if record else None)
        if parent_block:
            fields["parent_block_id"] = parent_block
        if not failed:
            diff = from_tool_input(tool, tool_input if isinstance(tool_input, dict) else {})
            if diff:
                fields["diff"] = diff
        return [Emit("tool_call", fields)]

    def _feed_user(self, message: UserMessage) -> list[Emit]:
        content = message.content
        if isinstance(content, str):
            return self._injected(message, content)
        emits: list[Emit] = []
        for block in content:
            if isinstance(block, ToolResultBlock):
                emits.extend(self._tool_result(block, message.parent_tool_use_id))
        return emits

    def _injected(self, message: UserMessage, text: str) -> list[Emit]:
        """Amendment A30: words another agent put into a session this device drives.

        The prompts this device sends are published under the request's own id
        before the SDK is touched, so the only user text worth a block here is
        the text nobody typed — a teammate's message, a task's notification.
        """
        said = classify({"origin": message.origin}, text)
        if said is None or not said.by_agent:
            return []
        return [
            Emit(
                "user_message",
                {
                    "block_id": message.uuid or f"agent:{uuid.uuid4()}",
                    "text": said.text,
                    "source": "agent",
                },
            )
        ]

    def _feed_system(self, message: SystemMessage) -> list[Emit]:
        data = message.data or {}
        if message.subtype == markers.COMPACT_BOUNDARY:
            # Amendment A32: the same line a mirrored session publishes for the
            # same row. The summary that follows is dropped by `_injected`,
            # which asks whose words a message holds and finds nobody's.
            return [Emit("notice", {"level": "info", "text": COMPACTION_NOTICE})]
        if message.subtype != "init":
            return []
        session_id = data.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        model = data.get("model")
        if isinstance(model, str) and model:
            self.model = model
            return [Emit("meta", {"model": model})]
        return []

    def _feed_result(self, message: ResultMessage) -> list[Emit]:
        if message.session_id:
            self.session_id = message.session_id
        if self._interrupted:
            stop_reason = "interrupted"
        elif message.subtype == "success" and not message.is_error:
            stop_reason = "completed"
        else:
            stop_reason = "error"
        raw_usage = message.usage or {}
        total = sum(
            value
            for key, value in raw_usage.items()
            if key.endswith("_tokens") and isinstance(value, int)
        )
        usage: dict[str, Any] = {
            "input_tokens": int(raw_usage.get("input_tokens") or 0),
            "output_tokens": int(raw_usage.get("output_tokens") or 0),
            "total_tokens": total,
        }
        if message.total_cost_usd is not None:
            usage["cost_usd"] = round(float(message.total_cost_usd), 6)
        fields: dict[str, Any] = {
            "stop_reason": stop_reason,
            "duration_ms": int(message.duration_ms),
        }
        if usage:
            fields["usage"] = usage
        # Amendment A35: the SDK reports the vendor's 429 as a status and not as
        # a time; the adapter reads the reset out of the session's transcript.
        limit = claude_result_limit(getattr(message, "api_error_status", None))
        if limit is not None:
            fields["stop_reason"] = "error"
            fields["limit"] = limit
        emits = [Emit("turn_completed", fields)]
        if message.is_error and message.result:
            said = without_paths(str(message.result))[:2000]
            emits.insert(0, Emit("error", {"message": said}))
        return emits

    def _feed_task(self, message: Any) -> list[Emit]:
        """Keep the `subagent` tool row in step with Task lifecycle messages."""
        tool_use_id = getattr(message, "tool_use_id", None)
        if not isinstance(tool_use_id, str) or tool_use_id not in self._tools:
            return []
        record = self._tools[tool_use_id]
        if record.get("finished"):
            # A later event replaces the block wholesale, so a progress message
            # arriving after the result would turn a finished row back into a
            # spinner.
            return []
        status = getattr(message, "status", None)
        description = getattr(message, "description", None) or getattr(message, "summary", None)
        fields: dict[str, Any] = {
            "block_id": tool_use_id,
            "tool": str(record["tool"]),
            "tool_kind": "subagent",
            "title": tool_title(str(record["tool"]), record["input"], self.cwd),
            "status": "succeeded" if status == "completed" else "running",
            "input": record["input"],
            "started_at": int(record["started_at"]),
        }
        if isinstance(description, str) and description:
            fields["summary"] = description[:500]
        if status in {"failed", "stopped"}:
            fields["status"] = "failed" if status == "failed" else "cancelled"
        return [Emit("tool_call", fields)]
