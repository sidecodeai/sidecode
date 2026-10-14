"""Translate Codex app-server notifications and rollout rows into events.

Live notifications use camelCase field names; rollout records on disk use
snake_case and PascalCase item types. Both are normalised into one shape here
so a single mapping table covers live sessions and mirrored terminal ones.
"""

from __future__ import annotations

import re
from typing import Any

from ...models import now_ms
from ...sessions.limits import codex_turn_limit
from ..base import COMPACTION_NOTICE, Emit

_SNAKE = re.compile(r"_([a-z0-9])")
MAX_OUTPUT_CHARS = 200_000

_TOOL_KIND_BY_ITEM = {
    "commandExecution": "shell",
    "fileChange": "edit",
    "mcpToolCall": "mcp",
    "dynamicToolCall": "other",
    "webSearch": "web",
    "subAgentActivity": "subagent",
    "collabAgentToolCall": "subagent",
    "imageView": "read",
    "imageGeneration": "other",
    "functionCallOutput": "other",
    "sleep": "other",
}
# Items that are a state change rather than work: a terminal draws a banner for
# each, so the apps get the one-line `notice` that says the same thing (A27).
_NOTICES = {
    "contextCompaction": COMPACTION_NOTICE,
    "enteredReviewMode": "Review started",
    "exitedReviewMode": "Review finished",
}
_STATUS_MAP = {
    "inProgress": "running",
    "completed": "succeeded",
    "failed": "failed",
    "declined": "cancelled",
}


def camel(name: str) -> str:
    return _SNAKE.sub(lambda match: match.group(1).upper(), name)


# Values under these keys are user data, not protocol structure: the `changes`
# map is keyed by file path and `arguments` by a tool's own parameter names, so
# camelCasing them would rewrite the paths and arguments we display.
_OPAQUE_KEYS = frozenset({"changes", "arguments", "input", "result", "env", "answers"})


def normalise(value: Any) -> Any:
    """Recursively camelCase structural dict keys so both record shapes match."""
    if isinstance(value, dict):
        return {
            camel(str(key)): (item if str(key) in _OPAQUE_KEYS else normalise(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalise(item) for item in value]
    return value


def item_type(item: dict[str, Any]) -> str:
    raw = str(item.get("type") or "")
    return raw[:1].lower() + raw[1:] if raw else ""


def text_of(content: Any) -> str:
    """The plain text of an item's `content`, whatever shape Codex sent it in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for entry in content:
            if isinstance(entry, str):
                parts.append(entry)
            elif isinstance(entry, dict):
                parts.append(str(entry.get("text") or ""))
        return "".join(part for part in parts if part)
    return ""


def _command_text(command: Any) -> str:
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _file_changes(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Both `changes` shapes: a list of `{path, diff}` or a path-keyed object."""
    changes = item.get("changes")
    result: list[dict[str, Any]] = []
    if isinstance(changes, list):
        for entry in changes:
            if isinstance(entry, dict):
                result.append(
                    {"path": str(entry.get("path") or ""), "diff": str(entry.get("diff") or "")}
                )
    elif isinstance(changes, dict):
        for path, entry in changes.items():
            if isinstance(entry, dict):
                diff = (
                    entry.get("unifiedDiff") or entry.get("unified_diff") or entry.get("diff") or ""
                )
                result.append({"path": str(path), "diff": str(diff)})
    return result


def _plan_items(plan: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for index, step in enumerate((plan or [])[:128]):
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or "pending")
        mapped = {"inProgress": "in_progress"}.get(status, status)
        if mapped not in {"pending", "in_progress", "completed"}:
            mapped = "pending"
        items.append({"id": str(index), "text": str(step.get("step") or ""), "status": mapped})
    return items


class CodexTranslator:
    """Stateful per-thread mapping from Codex events to protocol events."""

    def __init__(self, cwd: str | None = None, *, mirror_user_messages: bool = True) -> None:
        """`mirror_user_messages` turns Codex's own echo of the prompt into a `user_message`.

        A rollout read from disk is the only record of what the user typed in a terminal, so it
        mirrors. The live app-server stream echoes prompts the daemon itself sent, which the
        adapter has already published with the right `block_id` and `source`, so it does not.
        """
        self.cwd = cwd
        self.turn_id: str | None = None
        self.usage: dict[str, Any] = {}
        self._mirror_user_messages = mirror_user_messages
        # A review runs as a turn of its own on this thread, and the brief the
        # reviewer is given arrives as a `userMessage` item. Nobody typed it, so
        # it is not a bubble (A27).
        self._reviewing = False
        self._started_at: dict[str, int] = {}
        self._items: dict[str, dict[str, Any]] = {}
        self._output: dict[str, str] = {}

    def pending_diff(self, item_id: str) -> dict[str, Any] | None:
        """The `tool_call.diff` payload for a streamed item, if we have one."""
        item = self._items.get(item_id)
        if item is None:
            return None
        emit = self._tool_emit(
            item, item_type(item), item_id, self._started_at.get(item_id, 0), False, None
        )
        diff = emit.fields.get("diff")
        return diff if isinstance(diff, dict) else None

    # ------------------------------------------------------------ live feed

    def notification(self, method: str, params: dict[str, Any]) -> list[Emit]:
        data = normalise(params)
        if method == "item/agentMessage/delta":
            return self._delta("assistant_text", data)
        if method in {"item/reasoning/summaryTextDelta", "item/reasoning/textDelta"}:
            return self._delta("thinking", data)
        if method == "item/plan/delta":
            return []
        if method == "turn/plan/updated":
            return [Emit("todos", {"items": _plan_items(data.get("plan"))})]
        if method == "item/commandExecution/outputDelta":
            return self._output_delta(data)
        if method == "item/fileChange/patchUpdated":
            return self.item(dict(data, type="fileChange", id=data.get("itemId")), False)
        if method == "item/started":
            return self.item(data.get("item") or {}, False, data.get("startedAtMs"))
        if method == "item/completed":
            return self.item(data.get("item") or {}, True, data.get("completedAtMs"))
        if method == "turn/started":
            turn = data.get("turn") or {}
            self.turn_id = str(turn.get("id") or "") or None
            return []
        if method == "turn/completed":
            return self._turn_completed(data.get("turn") or {})
        if method == "thread/tokenUsage/updated":
            return self._token_usage(data.get("tokenUsage") or {})
        if method == "error":
            return [Emit("error", {"message": str(data.get("message") or "codex error")[:2000]})]
        return []

    def _delta(self, kind: str, data: dict[str, Any]) -> list[Emit]:
        delta = str(data.get("delta") or "")
        item_id = str(data.get("itemId") or "")
        if not delta or not item_id:
            return []
        return [Emit(kind, {"block_id": item_id, "delta": delta}, delta=True)]

    def _output_delta(self, data: dict[str, Any]) -> list[Emit]:
        """`tool_call` has no delta form, so republish the block with more output."""
        item_id = str(data.get("itemId") or "")
        delta = str(data.get("delta") or "")
        if not item_id or not delta:
            return []
        buffered = (self._output.get(item_id, "") + delta)[-MAX_OUTPUT_CHARS:]
        self._output[item_id] = buffered
        item = self._items.get(item_id)
        if item is None:
            return []
        return [
            self._tool_emit(
                dict(item, aggregatedOutput=buffered),
                item_type(item),
                item_id,
                self._started_at.get(item_id, now_ms()),
                False,
                None,
            )
        ]

    def _turn_completed(self, turn: dict[str, Any]) -> list[Emit]:
        status = str(turn.get("status") or "completed")
        stop_reason = {
            "completed": "completed",
            "interrupted": "interrupted",
            "failed": "error",
        }.get(status, "completed")
        fields: dict[str, Any] = {
            "stop_reason": stop_reason,
            "duration_ms": int(turn.get("durationMs") or 0),
        }
        # Amendment A35: the turn names the usage limit but not the window,
        # which the caller fills in from `account/rateLimits/read`.
        limit = codex_turn_limit(turn)
        if limit is not None:
            fields["limit"] = limit
        if self.usage:
            fields["usage"] = dict(self.usage)
        self.turn_id = None
        emits: list[Emit] = []
        error = turn.get("error")
        if isinstance(error, dict) and error.get("message"):
            emits.append(Emit("error", {"message": str(error["message"])[:2000]}))
        emits.append(Emit("turn_completed", fields))
        return emits

    def _token_usage(self, usage: dict[str, Any]) -> list[Emit]:
        total = usage.get("total") or {}
        window = usage.get("modelContextWindow")
        summary: dict[str, Any] = {
            "input_tokens": int(total.get("inputTokens") or 0),
            "output_tokens": int(total.get("outputTokens") or 0),
            "total_tokens": int(total.get("totalTokens") or 0),
        }
        if isinstance(window, int):
            summary["context_window"] = window
            if "total_tokens" in summary:
                summary["context_used"] = summary["total_tokens"]
        if summary:
            self.usage.update(summary)
        return []

    # ------------------------------------------------------------ item feed

    def item(
        self, raw: dict[str, Any], completed: bool, timestamp: int | None = None
    ) -> list[Emit]:
        item = normalise(raw)
        kind = item_type(item)
        item_id = str(item.get("id") or "")
        if not item_id:
            return []
        started_at = self._started_at.setdefault(item_id, int(timestamp or now_ms()))
        if kind in _TOOL_KIND_BY_ITEM:
            self._items[item_id] = item
            if completed:
                self._output.pop(item_id, None)
            elif self._output.get(item_id) and not item.get("aggregatedOutput"):
                item = dict(item, aggregatedOutput=self._output[item_id])

        if kind in _NOTICES:
            if kind == "enteredReviewMode":
                self._reviewing = True
            elif kind == "exitedReviewMode":
                self._reviewing = False
            # The item arrives twice, and a `notice` owns no block to replace,
            # so only the completion of it is published.
            return [Emit("notice", {"level": "info", "text": _NOTICES[kind]})] if completed else []

        if kind == "userMessage":
            if not self._mirror_user_messages or self._reviewing:
                return []
            text = text_of(item.get("content"))
            if not text:
                return []
            return [
                Emit(
                    "user_message",
                    {"block_id": item_id, "text": text, "source": "terminal"},
                )
            ]
        if kind == "agentMessage":
            if not completed:
                return []
            text = str(item.get("text") or "") or text_of(item.get("content"))
            if not text:
                return []
            return [Emit("assistant_text", {"block_id": item_id, "text": text, "done": True})]
        if kind == "reasoning":
            if not completed:
                return []
            summary = item.get("summary") or item.get("summaryText") or []
            text = text_of(summary) or text_of(item.get("content"))
            if not text:
                return []
            return [Emit("thinking", {"block_id": item_id, "text": text, "done": True})]
        if kind == "plan":
            return [Emit("todos", {"items": _plan_items(item.get("steps") or item.get("plan"))})]
        if kind in _TOOL_KIND_BY_ITEM:
            return [self._tool_emit(item, kind, item_id, started_at, completed, timestamp)]
        return []

    def _tool_emit(
        self,
        item: dict[str, Any],
        kind: str,
        item_id: str,
        started_at: int,
        completed: bool,
        timestamp: int | None,
    ) -> Emit:
        status = _STATUS_MAP.get(
            str(item.get("status") or ""), "running" if not completed else "succeeded"
        )
        fields: dict[str, Any] = {
            "block_id": item_id,
            "tool": self._tool_name(item, kind),
            "tool_kind": _TOOL_KIND_BY_ITEM.get(kind, "other"),
            "title": self._title(item, kind),
            "status": status,
            "started_at": started_at,
        }
        if kind == "commandExecution":
            fields["input"] = {
                "command": _command_text(item.get("command")),
                "cwd": str(item.get("cwd") or ""),
            }
            output = item.get("aggregatedOutput")
            if isinstance(output, str) and output:
                fields["output"] = output[:MAX_OUTPUT_CHARS]
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int):
                fields["summary"] = f"exit {exit_code}"
                if completed and exit_code != 0:
                    fields["status"] = "failed"
        elif kind == "fileChange":
            changes = _file_changes(item)
            if changes:
                first = changes[0]
                patch = "\n".join(entry["diff"] for entry in changes)
                fields["diff"] = {
                    "path": first["path"],
                    "additions": sum(
                        1
                        for line in patch.splitlines()
                        if line.startswith("+") and not line.startswith("+++")
                    ),
                    "deletions": sum(
                        1
                        for line in patch.splitlines()
                        if line.startswith("-") and not line.startswith("---")
                    ),
                    "patch": patch,
                }
                if len(changes) > 1:
                    fields["summary"] = f"{len(changes)} files"
        elif kind == "mcpToolCall":
            fields["input"] = {
                "server": str(item.get("server") or ""),
                "tool": str(item.get("tool") or ""),
                "arguments": item.get("arguments"),
            }
            result = item.get("result")
            if result is not None:
                fields["output"] = text_of(result) or str(result)[:MAX_OUTPUT_CHARS]
        elif kind == "webSearch":
            fields["input"] = {"query": str(item.get("query") or "")}
        if completed:
            ended_at = int(timestamp or now_ms())
            fields["ended_at"] = ended_at
            duration = item.get("durationMs")
            fields["duration_ms"] = (
                int(duration) if isinstance(duration, int) else max(0, ended_at - started_at)
            )
        return Emit("tool_call", fields)

    def _tool_name(self, item: dict[str, Any], kind: str) -> str:
        if kind == "mcpToolCall":
            return f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}"
        if kind == "commandExecution":
            return "Shell"
        if kind == "fileChange":
            return "Apply patch"
        return kind[:1].upper() + kind[1:]

    def _title(self, item: dict[str, Any], kind: str) -> str:
        if kind == "commandExecution":
            command = _command_text(item.get("command"))
            return command.splitlines()[0][:200] if command else "shell"
        if kind == "fileChange":
            changes = _file_changes(item)
            if not changes:
                return "apply patch"
            first = changes[0]["path"]
            return first if len(changes) == 1 else f"{first} +{len(changes) - 1} more"
        if kind == "webSearch":
            return str(item.get("query") or "web search")
        if kind == "mcpToolCall":
            return str(item.get("tool") or "mcp tool")
        if kind == "subAgentActivity":
            return str(item.get("agentPath") or "subagent")
        if kind == "imageView":
            return str(item.get("path") or "image")
        return kind
