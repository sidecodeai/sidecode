"""Turn pi's RPC events into protocol events.

Everything here is pure: it reads one event object and returns `Emit`s. pi's
streaming updates are delta-only — they carry neither the cumulative message
nor the partial content — so the text of a block is assembled from its deltas
under the `contentIndex` the delta names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...models import now_ms
from ..base import Emit

Handler = Callable[[dict[str, Any]], list[Emit]]

# Internal kinds the adapter consumes rather than publishes.
QUEUE = "pi.queue"

MAX_OUTPUT_CHARS = 200_000
MAX_TITLE_CHARS = 200

# pi's built-in tools (`read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`,
# plus `powershell` on Windows). Anything else is an extension's own tool.
_TOOL_KINDS = {
    "read": "read",
    "ls": "read",
    "bash": "shell",
    "powershell": "shell",
    "edit": "edit",
    "write": "write",
    "grep": "search",
    "find": "search",
}
# The argument each built-in tool names its subject with, most specific first.
_TITLE_KEYS = ("command", "pattern", "path", "glob", "query")
_STOP_REASONS = {
    "stop": "completed",
    "length": "completed",
    "toolUse": "completed",
    "error": "error",
    "aborted": "interrupted",
}
_DELTA_KINDS = {
    "text_start": "assistant_text",
    "text_delta": "assistant_text",
    "text_end": "assistant_text",
    "thinking_start": "thinking",
    "thinking_delta": "thinking",
    "thinking_end": "thinking",
}


def stop_reason(value: str) -> str:
    """pi's own reason for ending a message, as one of the protocol's three."""
    return _STOP_REASONS.get(value, "completed")


def text_of(content: Any) -> str:
    """The plain text of one pi content block, or of a list of them."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "".join(text_of(entry) for entry in content)
    return ""


def _first_line(value: str, limit: int = MAX_TITLE_CHARS) -> str:
    stripped = value.strip()
    return stripped.splitlines()[0][:limit] if stripped else ""


@dataclass(slots=True)
class _Stream:
    block_id: str
    kind: str
    text: str = ""


@dataclass(slots=True)
class _Tool:
    block_id: str
    tool: str
    tool_kind: str
    title: str
    started_at: int
    status: str = "running"
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    diff: dict[str, Any] | None = None


class PiTranslator:
    """Stateful per-session mapping from pi's RPC events to protocol events."""

    def __init__(self) -> None:
        self.stop_reason = "completed"
        self._message = 0
        self._streams: dict[int, _Stream] = {}
        self._tools: dict[str, _Tool] = {}
        self._handlers: dict[str, Handler] = {
            "message_start": self._on_message_start,
            "message_update": self._on_message_update,
            "message_end": self._on_message_end,
            "turn_end": self._on_turn_end,
            "agent_settled": self._on_agent_settled,
            "tool_execution_start": self._on_tool_start,
            "tool_execution_update": self._on_tool_update,
            "tool_execution_end": self._on_tool_end,
            "queue_update": self._on_queue_update,
            "compaction_end": self._on_compaction_end,
            "auto_retry_start": self._on_auto_retry,
            "extension_error": self._on_extension_error,
        }

    # ------------------------------------------------------------ entry point

    def event(self, event: dict[str, Any]) -> list[Emit]:
        handler = self._handlers.get(str(event.get("type") or ""))
        return handler(event) if handler is not None else []

    def close_streams(self) -> list[Emit]:
        """Finish every open text block, which a torn-down session must also do."""
        return [self._close(index) for index in sorted(self._streams)]

    # ---------------------------------------------------------------- streams

    def _on_message_start(self, event: dict[str, Any]) -> list[Emit]:
        emits = self.close_streams()
        self._message += 1
        return emits

    def _on_message_update(self, event: dict[str, Any]) -> list[Emit]:
        delta = event.get("assistantMessageEvent")
        if not isinstance(delta, dict):
            return []
        kind = _DELTA_KINDS.get(str(delta.get("type") or ""))
        if kind is None:
            # `toolcall_*` deltas stream arguments the tool events carry whole.
            return []
        index = delta.get("contentIndex")
        index = index if isinstance(index, int) else 0
        if str(delta.get("type")).endswith("_end"):
            stream = self._streams.get(index)
            if stream is None:
                return []
            content = delta.get("content")
            if isinstance(content, str) and content:
                stream.text = content
            return [self._close(index)]
        text = str(delta.get("delta") or "")
        stream = self._open(index, kind)
        if not text:
            return []
        stream.text += text
        return [Emit(kind, {"block_id": stream.block_id, "delta": text}, delta=True)]

    def _open(self, index: int, kind: str) -> _Stream:
        stream = self._streams.get(index)
        if stream is None or stream.kind != kind:
            stream = _Stream(block_id=f"m{self._message}:{index}:{kind}", kind=kind)
            self._streams[index] = stream
        return stream

    def _close(self, index: int) -> Emit:
        stream = self._streams.pop(index)
        return Emit(stream.kind, {"block_id": stream.block_id, "text": stream.text, "done": True})

    def _on_message_end(self, event: dict[str, Any]) -> list[Emit]:
        emits = self.close_streams()
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
        reason = str(message.get("stopReason") or "")
        if reason:
            self.stop_reason = stop_reason(reason)
        failure = message.get("errorMessage")
        if reason == "error" and isinstance(failure, str) and failure:
            emits.append(Emit("error", {"message": failure[:2000]}))
        return emits

    # ------------------------------------------------------------- tool calls

    def _on_tool_start(self, event: dict[str, Any]) -> list[Emit]:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return []
        tool = self._tool(event, call_id)
        return [_tool_emit(tool, ended=False)]

    def _on_tool_update(self, event: dict[str, Any]) -> list[Emit]:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return []
        tool = self._tool(event, call_id)
        partial = event.get("partialResult")
        if isinstance(partial, dict):
            # pi sends the output accumulated so far, not the delta.
            tool.output = text_of(partial.get("content"))[:MAX_OUTPUT_CHARS]
        return [_tool_emit(tool, ended=False)]

    def _on_tool_end(self, event: dict[str, Any]) -> list[Emit]:
        call_id = str(event.get("toolCallId") or "")
        if not call_id:
            return []
        tool = self._tool(event, call_id)
        tool.status = "failed" if event.get("isError") else "succeeded"
        result = event.get("result")
        if isinstance(result, dict):
            tool.output = text_of(result.get("content"))[:MAX_OUTPUT_CHARS]
            tool.diff = _diff(result.get("details"), tool.input.get("path"))
        self._tools.pop(call_id, None)
        return [_tool_emit(tool, ended=True)]

    def _tool(self, event: dict[str, Any], call_id: str) -> _Tool:
        """The call this event belongs to, started here when it was not seen."""
        args = event.get("args")
        args = args if isinstance(args, dict) else {}
        name = str(event.get("toolName") or "tool")
        tool = self._tools.get(call_id)
        if tool is None:
            tool = _Tool(
                block_id=call_id,
                tool=name,
                tool_kind=tool_kind(name),
                title=tool_title(args, name),
                started_at=now_ms(),
            )
            self._tools[call_id] = tool
        if args:
            tool.input = args
            tool.title = tool_title(args, tool.tool)
        return tool

    # ------------------------------------------------------------- turn state

    def _on_turn_end(self, event: dict[str, Any]) -> list[Emit]:
        message = event.get("message")
        reason = str(message.get("stopReason") or "") if isinstance(message, dict) else ""
        if reason:
            self.stop_reason = stop_reason(reason)
        return []

    def _on_agent_settled(self, event: dict[str, Any]) -> list[Emit]:
        """The run is over: no retry, no compaction and no queued message is left."""
        emits = self.close_streams()
        emits.append(Emit("turn_completed", {"stop_reason": self.stop_reason}))
        return emits

    def _on_queue_update(self, event: dict[str, Any]) -> list[Emit]:
        steering = [str(text) for text in event.get("steering") or [] if isinstance(text, str)]
        follow_up = [str(text) for text in event.get("followUp") or [] if isinstance(text, str)]
        return [Emit(QUEUE, {"steering": steering, "follow_up": follow_up})]

    # --------------------------------------------------------------- notices

    def _on_compaction_end(self, event: dict[str, Any]) -> list[Emit]:
        failure = event.get("errorMessage")
        if isinstance(failure, str) and failure:
            return [Emit("notice", {"level": "error", "text": failure[:500]})]
        if event.get("aborted"):
            return [Emit("notice", {"level": "info", "text": "Compaction was stopped."})]
        return [
            Emit(
                "notice",
                {"level": "warn", "text": "Context was compacted; earlier turns are summarised."},
            )
        ]

    def _on_auto_retry(self, event: dict[str, Any]) -> list[Emit]:
        return [
            Emit("notice", {"level": "warn", "text": "The model errored; pi is retrying."}),
        ]

    def _on_extension_error(self, event: dict[str, Any]) -> list[Emit]:
        message = str(event.get("error") or event.get("message") or "a pi extension failed")
        return [Emit("error", {"message": message[:2000]})]


def tool_kind(name: str) -> str:
    """The protocol kind of one of pi's tools; anything unknown is an extension's."""
    return _TOOL_KINDS.get(name, "other")


def tool_title(args: dict[str, Any], name: str) -> str:
    """What the call is about, from the argument the tool names its subject with."""
    for key in _TITLE_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _first_line(value)
    # An extension's tool names its arguments whatever it likes, so the first
    # thing it was given reads better than the tool's own name.
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return _first_line(value)
    return name


def _diff(details: Any, path: Any) -> dict[str, Any] | None:
    """pi's `edit` tool returns the unified patch it applied; others return none."""
    if not isinstance(details, dict):
        return None
    patch = details.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        return None
    lines = patch.splitlines()
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {
        "path": str(path or ""),
        "additions": added,
        "deletions": removed,
        "patch": patch,
    }


def _tool_emit(tool: _Tool, *, ended: bool) -> Emit:
    fields: dict[str, Any] = {
        "block_id": tool.block_id,
        "tool": tool.tool,
        "tool_kind": tool.tool_kind,
        "title": tool.title or tool.tool,
        "status": tool.status,
        "started_at": tool.started_at,
    }
    if tool.input:
        fields["input"] = tool.input
    if tool.output:
        fields["output"] = tool.output
    if tool.diff:
        fields["diff"] = tool.diff
    if ended:
        finished = now_ms()
        fields["ended_at"] = finished
        fields["duration_ms"] = max(0, finished - tool.started_at)
    return Emit("tool_call", fields)
