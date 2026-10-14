"""Map Claude tool names to the protocol's tool kinds and one-line titles."""

from __future__ import annotations

import os
from typing import Any

_KIND_BY_TOOL = {
    "Bash": "shell",
    "BashOutput": "shell",
    "KillShell": "shell",
    "Read": "read",
    "NotebookRead": "read",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Write": "write",
    "Grep": "search",
    "Glob": "search",
    "WebFetch": "web",
    "WebSearch": "web",
    "Task": "subagent",
    "Agent": "subagent",
    "TodoWrite": "todo",
}


def tool_kind(tool: str) -> str:
    if tool in _KIND_BY_TOOL:
        return _KIND_BY_TOOL[tool]
    if tool.startswith("mcp__"):
        return "mcp"
    return "other"


def _relative(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1 :]
    return path


def tool_title(tool: str, tool_input: dict[str, Any], cwd: str | None = None) -> str:
    """A short human summary: the command line, path or query behind the call."""
    if tool in {"Bash", "BashOutput", "KillShell"}:
        command = str(tool_input.get("command") or tool_input.get("description") or "").strip()
        return command.splitlines()[0][:200] if command else tool
    if tool in {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        return _relative(path, cwd) or tool
    if tool == "Grep":
        pattern = str(tool_input.get("pattern") or "")
        where = str(tool_input.get("path") or "")
        return f"{pattern} in {_relative(where, cwd)}" if where else pattern or tool
    if tool == "Glob":
        return str(tool_input.get("pattern") or tool)
    if tool == "WebFetch":
        return str(tool_input.get("url") or tool)
    if tool == "WebSearch":
        return str(tool_input.get("query") or tool)
    if tool in {"Task", "Agent"}:
        description = str(tool_input.get("description") or tool_input.get("prompt") or "")
        return description.splitlines()[0][:200] if description else tool
    if tool.startswith("mcp__"):
        return tool.split("__", 2)[-1]
    for key in ("description", "path", "file_path", "query", "pattern", "url"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return os.path.basename(value) if key.endswith("path") else value.splitlines()[0][:200]
    return tool


def todos_from_input(tool_input: dict[str, Any]) -> list[dict[str, str]]:
    """Normalise a TodoWrite payload into the protocol's `todos.items`."""
    raw = tool_input.get("todos")
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for index, entry in enumerate(raw[:100]):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("content") or entry.get("text") or entry.get("activeForm") or "")
        status = str(entry.get("status") or "pending")
        if status not in {"pending", "in_progress", "completed"}:
            status = "pending"
        items.append({"id": str(entry.get("id") or index), "text": text, "status": status})
    return items
