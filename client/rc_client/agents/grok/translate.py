"""Turn Grok's ACP session updates into protocol events.

The live stdio stream and the `updates.jsonl` Grok writes for every session
carry the *same* update objects under different envelopes, so one translator
serves a session this device drives and one a person is running in a terminal.
Everything here is pure: it reads an update and returns `Emit`s.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...models import now_ms
from ..base import Emit

Handler = Callable[[dict[str, Any], dict[str, Any]], list[Emit]]

# Internal kinds the adapter and the mirror consume rather than publish.
SETTINGS = "grok.settings"

# The three envelopes an update arrives in. `session/update` is standard ACP;
# the `_x.ai` ones carry Grok's own additions, including the end of a turn.
UPDATE_METHODS = frozenset({"session/update", "_x.ai/session/update", "_x.ai/session_notification"})

MAX_OUTPUT_CHARS = 200_000
USD_PER_TICK = 1e-10

_TOOL_KIND_BY_ACP = {
    "execute": "shell",
    "read": "read",
    "edit": "edit",
    "delete": "edit",
    "move": "edit",
    "search": "search",
    "fetch": "web",
    "think": "other",
    "switch_mode": "other",
    "other": "other",
}
_TOOL_KIND_BY_NAME = {
    "run_terminal_command": "shell",
    "read_file": "read",
    "list_dir": "read",
    "search_replace": "edit",
    "write": "write",
    "grep": "search",
    "web_search": "web",
    "spawn_subagent": "subagent",
    "todo_write": "todo",
}
_STATUS_MAP = {
    "pending": "running",
    "in_progress": "running",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}
_STOP_REASONS = {
    "end_turn": "completed",
    "cancelled": "interrupted",
    "canceled": "interrupted",
    "refusal": "error",
    "error": "error",
}


def stop_reason(value: str) -> str:
    """Grok's own reason for ending a turn, as one of the protocol's three."""
    return _STOP_REASONS.get(value, "completed")


def session_update(method: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The `(sessionUpdate, update)` pair of an update notification, if it is one."""
    if method not in UPDATE_METHODS:
        return None
    update = params.get("update")
    if not isinstance(update, dict):
        return None
    kind = str(update.get("sessionUpdate") or "")
    return (kind, update) if kind else None


def text_of(content: Any) -> str:
    """The plain text of an ACP content block, or of a list of them."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "content":
            return text_of(content.get("content"))
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "".join(text_of(entry) for entry in content)
    return ""


def _first_line(text: str, limit: int = 200) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0][:limit] if stripped else ""


@dataclass(slots=True)
class _Stream:
    block_id: str
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
    summary: str = ""
    diff: dict[str, Any] | None = None


class GrokTranslator:
    """Stateful per-session mapping from ACP updates to protocol events."""

    def __init__(
        self,
        cwd: str | None = None,
        *,
        mirror_user_messages: bool = True,
        keep_replay: bool = False,
    ) -> None:
        """`mirror_user_messages` publishes Grok's echo of the prompt as a bubble.

        A terminal session's update log is the only record of what a person
        typed, so it mirrors. A session this device drives has already published
        the bubble with its own `block_id`, so it does not.

        `keep_replay` lets the history `session/load` replays through. A private
        child replays a conversation the apps already have; a session joined on
        the leader may be one this device has never seen, so its caller keeps
        the rows and decides for itself which are news (A28).
        """
        self.cwd = cwd
        self.usage: dict[str, Any] = {}
        self.context_window: int | None = None
        self._mirror_user_messages = mirror_user_messages
        self._keep_replay = keep_replay
        self._open: dict[str, _Stream] = {}
        self._tools: dict[str, _Tool] = {}
        self._prompt_id = ""
        self._handlers: dict[str, Handler] = {
            "agent_message_chunk": self._on_agent_message_chunk,
            "agent_thought_chunk": self._on_agent_thought_chunk,
            "user_message_chunk": self._on_user_message_chunk,
            "tool_call": self._on_tool_call,
            "tool_call_update": self._on_tool_call_update,
            "plan": self._on_plan,
            "turn_completed": self._on_turn_completed,
            "model_changed": self._on_model_changed,
            "current_mode_update": self._on_current_mode_update,
            "config_option_update": self._on_config_option_update,
            "error": self._on_error,
        }

    # ------------------------------------------------------------- entry point

    def notification(self, method: str, params: dict[str, Any]) -> list[Emit]:
        found = session_update(method, params)
        if found is None:
            return []
        kind, update = found
        meta = params.get("_meta")
        meta = meta if isinstance(meta, dict) else {}
        if not self._keep_replay and (meta.get("isReplay") or update.get("isReplay")):
            # `session/load` replays the whole conversation; the device already
            # has every one of those events under its own block ids.
            return []
        self._note_context(meta)
        handler = self._handlers.get(kind)
        return handler(update, meta) if handler is not None else []

    def close_streams(self) -> list[Emit]:
        """Finish every open text block, which a torn-down session must also do."""
        return [self._close(kind) for kind in list(self._open)]

    # ---------------------------------------------------------------- streams

    def _stream_key(self, kind: str, meta: dict[str, Any]) -> str:
        prompt = str(meta.get("promptId") or self._prompt_id or "turn")
        start = str(meta.get("streamStartMs") or meta.get("eventId") or "0")
        return f"{prompt}:{start}:{kind}"

    def _chunk(self, kind: str, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        text = text_of(update.get("content"))
        if not text:
            return []
        block_id = self._stream_key(kind, meta)
        emits: list[Emit] = []
        open_stream = self._open.get(kind)
        if open_stream is not None and open_stream.block_id != block_id:
            emits.append(self._close(kind))
            open_stream = None
        if open_stream is None:
            open_stream = _Stream(block_id=block_id)
            self._open[kind] = open_stream
        open_stream.text += text
        emits.append(Emit(kind, {"block_id": block_id, "delta": text}, delta=True))
        return emits

    def _close(self, kind: str) -> Emit:
        stream = self._open.pop(kind)
        return Emit(kind, {"block_id": stream.block_id, "text": stream.text, "done": True})

    def _on_agent_message_chunk(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        return self._chunk("assistant_text", update, meta)

    def _on_agent_thought_chunk(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        return self._chunk("thinking", update, meta)

    def _on_user_message_chunk(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        if not self._mirror_user_messages:
            return []
        text = text_of(update.get("content"))
        if not text:
            return []
        prompt_id = str(meta.get("promptId") or meta.get("eventId") or now_ms())
        fields = {"block_id": f"user:{prompt_id}", "text": text, "source": "terminal"}
        return [Emit("user_message", fields)]

    # ------------------------------------------------------------- tool calls

    def _on_tool_call(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return []
        descriptor = self._descriptor(update)
        name = str(descriptor.get("name") or update.get("title") or "tool")
        tool = _Tool(
            block_id=call_id,
            tool=name,
            tool_kind=self._tool_kind(update, descriptor),
            title=self._title(update, name),
            started_at=int(meta.get("agentTimestampMs") or now_ms()),
        )
        self._tools[call_id] = tool
        self._apply_tool(tool, update)
        return [self._tool_emit(tool, ended=False)]

    def _on_tool_call_update(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return []
        tool = self._tools.get(call_id)
        if tool is None:
            # A status update for a call started before this mirror attached.
            descriptor = self._descriptor(update)
            name = str(descriptor.get("name") or update.get("title") or "tool")
            tool = _Tool(
                block_id=call_id,
                tool=name,
                tool_kind=self._tool_kind(update, descriptor),
                title=self._title(update, name),
                started_at=int(meta.get("agentTimestampMs") or now_ms()),
            )
            self._tools[call_id] = tool
        self._apply_tool(tool, update)
        ended = tool.status in {"succeeded", "failed", "cancelled"}
        emit = self._tool_emit(tool, ended=ended, ended_at=meta.get("agentTimestampMs"))
        if ended:
            self._tools.pop(call_id, None)
        return [emit]

    @staticmethod
    def _descriptor(update: dict[str, Any]) -> dict[str, Any]:
        """Grok names the real tool under `_meta["x.ai/tool"]`; ACP has no field for it."""
        meta = update.get("_meta")
        descriptor = meta.get("x.ai/tool") if isinstance(meta, dict) else None
        return descriptor if isinstance(descriptor, dict) else {}

    @staticmethod
    def _tool_kind(update: dict[str, Any], descriptor: dict[str, Any]) -> str:
        name = str(descriptor.get("name") or "")
        if name in _TOOL_KIND_BY_NAME:
            return _TOOL_KIND_BY_NAME[name]
        namespace = str(descriptor.get("namespace") or "")
        if namespace and namespace != "grok_build":
            return "mcp"
        acp_kind = str(update.get("kind") or descriptor.get("kind") or "").lower()
        return _TOOL_KIND_BY_ACP.get(acp_kind, "other")

    def _title(self, update: dict[str, Any], name: str) -> str:
        raw = update.get("rawInput")
        if isinstance(raw, dict):
            for key in ("command", "file_path", "path", "pattern", "query", "description"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    return _first_line(value)
        return _first_line(str(update.get("title") or name)) or name

    def _apply_tool(self, tool: _Tool, update: dict[str, Any]) -> None:
        status = str(update.get("status") or "")
        if status:
            tool.status = _STATUS_MAP.get(status, tool.status)
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict):
            tool.input = {key: value for key, value in raw_input.items() if key != "variant"}
            tool.title = self._title(update, tool.tool)
        locations = update.get("locations")
        if isinstance(locations, list) and locations:
            # `tool_call` has no field of its own for these, and `input` is the
            # one open object an app already renders.
            tool.input = dict(tool.input, locations=locations[:32])
        output, summary = self._output(update)
        if output:
            tool.output = output[:MAX_OUTPUT_CHARS]
        if summary:
            tool.summary = summary
        diff = self._diff(update)
        if diff is not None:
            tool.diff = diff

    @staticmethod
    def _output(update: dict[str, Any]) -> tuple[str, str]:
        """Readable output and a badge, never the raw byte array Grok also sends."""
        raw = update.get("rawOutput")
        summary = ""
        text = ""
        if isinstance(raw, dict):
            for key in ("output_for_prompt", "text", "content"):
                value = raw.get(key)
                if isinstance(value, str) and value:
                    text = value
                    break
            exit_code = raw.get("exit_code")
            if isinstance(exit_code, int):
                summary = f"exit {exit_code}"
        if not text:
            content = update.get("content")
            if isinstance(content, list):
                text = "".join(
                    text_of(entry)
                    for entry in content
                    if isinstance(entry, dict) and entry.get("type") == "content"
                )
        return text, summary

    def _diff(self, update: dict[str, Any]) -> dict[str, Any] | None:
        """An ACP `diff` content block, rendered as the unified patch apps show."""
        content = update.get("content")
        if not isinstance(content, list):
            return None
        for entry in content:
            if not isinstance(entry, dict) or entry.get("type") != "diff":
                continue
            path = str(entry.get("path") or "")
            if not path:
                continue
            old = str(entry.get("oldText") or "")
            new = str(entry.get("newText") or "")
            patch = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    new.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            lines = patch.splitlines()
            additions = sum(
                1 for line in lines if line.startswith("+") and not line.startswith("+++")
            )
            deletions = sum(
                1 for line in lines if line.startswith("-") and not line.startswith("---")
            )
            return {"path": path, "additions": additions, "deletions": deletions, "patch": patch}
        return None

    @staticmethod
    def _tool_emit(tool: _Tool, *, ended: bool, ended_at: Any = None) -> Emit:
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
        if tool.summary:
            fields["summary"] = tool.summary
        if tool.diff:
            fields["diff"] = tool.diff
        if ended:
            finished = int(ended_at or now_ms())
            fields["ended_at"] = finished
            fields["duration_ms"] = max(0, finished - tool.started_at)
        return Emit("tool_call", fields)

    # ------------------------------------------------------------------ plans

    def _on_plan(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        entries = update.get("entries")
        items: list[dict[str, str]] = []
        for index, entry in enumerate((entries or [])[:128]):
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "pending")
            if status not in {"pending", "in_progress", "completed"}:
                status = "pending"
            text = str(entry.get("content") or entry.get("title") or "")
            items.append({"id": str(entry.get("id") or index), "text": text, "status": status})
        return [Emit("todos", {"items": items})]

    # ------------------------------------------------------------- turn state

    def _note_context(self, meta: dict[str, Any]) -> None:
        total = meta.get("totalTokens")
        if isinstance(total, int) and total > 0:
            self.usage["context_used"] = total
            if self.context_window:
                self.usage["context_window"] = self.context_window

    def _on_turn_completed(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        emits = self.close_streams()
        self._add_usage(update.get("usage"))
        fields: dict[str, Any] = {
            "stop_reason": stop_reason(str(update.get("stop_reason") or "")),
            "duration_ms": int(update.get("elapsed_ms") or 0),
        }
        if self.usage:
            fields["usage"] = dict(self.usage)
        emits.append(Emit("turn_completed", fields))
        return emits

    def _add_usage(self, usage: Any) -> None:
        """Grok reports one turn at a time; a session shows what it has spent."""
        if not isinstance(usage, dict):
            return
        for field_name, key in (
            ("input_tokens", "inputTokens"),
            ("output_tokens", "outputTokens"),
            ("total_tokens", "totalTokens"),
        ):
            value = usage.get(key)
            if isinstance(value, int):
                self.usage[field_name] = int(self.usage.get(field_name) or 0) + value
        ticks = usage.get("costUsdTicks")
        if isinstance(ticks, int):
            self.usage["cost_usd"] = round(
                float(self.usage.get("cost_usd") or 0.0) + ticks * USD_PER_TICK, 8
            )
        if self.context_window:
            self.usage["context_window"] = self.context_window

    def _on_error(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        message = str(update.get("message") or update.get("error") or "grok error")
        return [Emit("error", {"message": message[:2000]})]

    # ---------------------------------------------------------------- settings

    def _on_model_changed(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        fields: dict[str, Any] = {}
        model = update.get("model_id") or update.get("modelId")
        if isinstance(model, str) and model:
            fields["model"] = model
        effort = update.get("reasoning_effort") or update.get("reasoningEffort")
        if isinstance(effort, str) and effort:
            fields["effort"] = effort
        return [Emit(SETTINGS, fields)] if fields else []

    def _on_current_mode_update(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        mode = update.get("currentModeId") or update.get("current_mode_id")
        if not isinstance(mode, str) or not mode:
            return []
        return [Emit(SETTINGS, {"permission_mode": mode})]

    def _on_config_option_update(self, update: dict[str, Any], meta: dict[str, Any]) -> list[Emit]:
        fields = config_settings(update.get("configOptions"))
        return [Emit(SETTINGS, fields)] if fields else []


def config_settings(options: Any) -> dict[str, Any]:
    """`model` and `effort` as the agent's own `configOptions` list reports them."""
    fields: dict[str, Any] = {}
    if not isinstance(options, list):
        return fields
    for option in options:
        if not isinstance(option, dict):
            continue
        value = option.get("currentValue")
        if not isinstance(value, str) or not value:
            continue
        if option.get("id") == "model":
            fields["model"] = value
        elif option.get("id") == "reasoning_effort":
            fields["effort"] = value
    return fields
