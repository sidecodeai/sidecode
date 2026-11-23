"""Turn `agy`'s ACP session updates into protocol events.

`agy` streams a reply as text chunks with no id of their own, so a text block runs until a new
tool begins or the turn ends, and each tool is one block under the id `agy` gives it, the id a
replay names it by as well. A tool's row is titled from the absolute path or the command it acts
on, not from `agy`'s own title, which names a file relative to the session's folder. Nothing here
publishes: it turns one update into `Emit`s, and says what the session has running.
"""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass, field
from typing import Any

from ...attachments import without_paths
from ...models import now_ms
from ..base import Emit

MAX_OUTPUT_CHARS = 200_000
# `agy`'s ACP tool kinds, as the protocol's (5.5).
_KIND_BY_ACP = {
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
# The tools whose name says more than their kind: `write_file` creates files, under kind `edit`.
_KIND_BY_NAME = {"run_command": "shell", "read_file": "read", "write_file": "write"}
_STATUS = {
    "pending": "running",
    "in_progress": "running",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}
_ENDED = frozenset({"succeeded", "failed", "cancelled"})
_STOP_REASONS = {"end_turn": "completed", "cancelled": "interrupted"}
# The tool that wakes `agy` up later by itself.
SCHEDULE = "schedule"
# Where `agy` names the file a tool acts on, absolute.
_PATH_KEYS = ("Path", "AbsolutePath", "TargetFile", "path", "file_path")


def stop_reason(value: str) -> str:
    """`end_turn` is a completed turn, `cancelled` an interrupted one; any other end an error."""
    return _STOP_REASONS.get(value, "error")


def text_of(content: Any) -> str:
    """The plain text of an ACP content block, or of a list of them."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "content":
            return text_of(content.get("content"))
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        return "".join(text_of(entry) for entry in content)
    return ""


def _first_line(text: str, limit: int = 200) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0][:limit] if stripped else ""


@dataclass(slots=True)
class Tool:
    """One tool call as the apps see it, built up from every update `agy` sends for it."""

    block_id: str
    tool: str
    tool_kind: str
    started_at: int
    title: str = ""
    status: str = "running"
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    summary: str = ""
    diff: dict[str, Any] | None = None
    # A command `agy` runs as a daemon, a dev server say, which nothing waits for.
    daemon: bool = False

    @property
    def ended(self) -> bool:
        return self.status in _ENDED

    @property
    def lingers(self) -> bool:
        """What keeps `agy` going after its prompt answered: a command it runs on in the
        background that is not a daemon, or a scheduled wake (A64)."""
        return (self.tool_kind == "shell" and not self.daemon) or self.tool == SCHEDULE

    def apply(self, update: dict[str, Any]) -> None:
        status = str(update.get("status") or "")
        if status:
            self.status = _STATUS.get(status, self.status)
        raw = update.get("rawInput")
        if isinstance(raw, dict):
            self.input = {
                key: without_paths(value) if isinstance(value, str) else value
                for key, value in raw.items()
            }
            self.daemon = raw.get("IsDaemon") is True
        title = _title(update)
        if title:
            self.title = title
        output, summary = _output(update)
        if output:
            self.output = output[:MAX_OUTPUT_CHARS]
        if summary:
            self.summary = summary
        diff = _diff(update)
        if diff is not None:
            self.diff = diff

    def emit(self, ended_at: int | None = None) -> Emit:
        fields: dict[str, Any] = {
            "block_id": self.block_id,
            "tool": self.tool,
            "tool_kind": self.tool_kind,
            "title": self.title or self.tool,
            "status": self.status,
            "started_at": self.started_at,
        }
        for key, value in (
            ("input", self.input),
            ("output", self.output),
            ("summary", self.summary),
            ("diff", self.diff),
        ):
            if value:
                fields[key] = value
        if self.ended:
            finished = ended_at or now_ms()
            fields["ended_at"] = finished
            fields["duration_ms"] = max(0, finished - self.started_at)
        return Emit("tool_call", fields)


def new_tool(update: dict[str, Any]) -> Tool:
    """The block for a tool call `agy` has just announced, or replayed in its final state."""
    name = str(update.get("name") or update.get("title") or "tool")
    tool = Tool(
        block_id=str(update.get("toolCallId") or ""),
        tool=name,
        tool_kind=_kind(update, name),
        started_at=now_ms(),
    )
    tool.apply(update)
    return tool


def _kind(update: dict[str, Any], name: str) -> str:
    meta = update.get("_meta")
    if isinstance(meta, dict) and meta.get("is_mcp_tool_call") is True:
        return "mcp"
    if name in _KIND_BY_NAME:
        return _KIND_BY_NAME[name]
    return _KIND_BY_ACP.get(str(update.get("kind") or "").lower(), "other")


def _title(update: dict[str, Any]) -> str:
    raw = update.get("rawInput")
    raw = raw if isinstance(raw, dict) else {}
    command = raw.get("CommandLine")
    if isinstance(command, str) and command.strip():
        return without_paths(_first_line(command))
    locations = update.get("locations")
    if isinstance(locations, list) and locations and isinstance(locations[0], dict):
        path = locations[0].get("path")
        if isinstance(path, str) and path:
            return without_paths(path)
    for key in _PATH_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return without_paths(_first_line(value))
    return without_paths(_first_line(str(update.get("title") or "")))


def _output(update: dict[str, Any]) -> tuple[str, str]:
    raw = update.get("rawOutput")
    text = ""
    summary = ""
    if isinstance(raw, dict):
        value = raw.get("output")
        if isinstance(value, str):
            text = value
        exit_code = raw.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            summary = f"exit {exit_code}"
    if not text:
        content = update.get("content")
        if isinstance(content, list):
            text = "".join(
                text_of(entry)
                for entry in content
                if isinstance(entry, dict) and entry.get("type") == "content"
            )
    return without_paths(text), summary


def _diff(update: dict[str, Any]) -> dict[str, Any] | None:
    """An ACP `diff` block as the unified patch the apps show; a new file has no old text."""
    content = update.get("content")
    for entry in content if isinstance(content, list) else []:
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
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        return {"path": path, "additions": added, "deletions": removed, "patch": patch}
    return None


@dataclass(slots=True)
class _Stream:
    block_id: str
    text: str = ""


class Translator:
    """What one session has streaming and running, turned into protocol events."""

    def __init__(self) -> None:
        self._prefix = uuid.uuid4().hex[:12]
        self._count = 0
        self._open: dict[str, _Stream] = {}
        self._tools: dict[str, Tool] = {}

    def text(self, kind: str, text: str) -> tuple[list[Emit], bool]:
        """A chunk of `assistant_text` or `thinking`, and whether it began a new block."""
        if not text:
            return [], False
        stream = self._open.get(kind)
        opened = stream is None
        if stream is None:
            self._count += 1
            stream = _Stream(f"{self._prefix}:{self._count}:{kind}")
            self._open[kind] = stream
        stream.text += text
        return [Emit(kind, {"block_id": stream.block_id, "delta": text}, delta=True)], opened

    def close_streams(self) -> list[Emit]:
        """Finish every open text block: a tool began, or the turn ended."""
        emits = [
            Emit(kind, {"block_id": stream.block_id, "text": stream.text, "done": True})
            for kind, stream in self._open.items()
        ]
        self._open.clear()
        return emits

    def tool(self, update: dict[str, Any]) -> tuple[list[Emit], bool]:
        """A `tool_call` or `tool_call_update`, and whether the tool is new to this session."""
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return [], False
        found = self._tools.get(call_id)
        fresh = found is None
        emits: list[Emit] = []
        if found is None:
            emits = self.close_streams()
            found = new_tool(update)
            self._tools[call_id] = found
        else:
            found.apply(update)
        emits.append(found.emit())
        if found.ended:
            self._tools.pop(call_id, None)
        return emits, fresh

    def knows(self, call_id: str) -> bool:
        return call_id in self._tools

    def lingering(self) -> bool:
        """Whether a background command or a scheduled wake of `agy`'s is still running (A64)."""
        return any(tool.lingers and not tool.ended for tool in self._tools.values())

    def forget(self) -> None:
        """The process is gone, and everything it was running with it."""
        self._tools.clear()
        self._open.clear()
