"""Unified diffs for edit and write tool calls."""

from __future__ import annotations

import difflib
from typing import Any

MAX_DIFF_LINES = 4000


def unified(path: str, before: str, after: str) -> dict[str, Any]:
    """A `tool_call.diff` payload: path, line counts and the patch text."""
    before_lines = before.splitlines(keepends=True) if before else []
    after_lines = after.splitlines(keepends=True) if after else []
    lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = [*lines[:MAX_DIFF_LINES], "…\n"]
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    patch = "".join(lines)
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return {"path": path, "additions": additions, "deletions": deletions, "patch": patch}


def from_tool_input(tool: str, tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Build a diff for Claude's Edit / MultiEdit / Write tool inputs."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not path:
        return None
    if tool == "Write":
        return unified(path, "", str(tool_input.get("content") or ""))
    if tool == "Edit":
        return unified(
            path,
            str(tool_input.get("old_string") or ""),
            str(tool_input.get("new_string") or ""),
        )
    if tool == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        merged: dict[str, Any] = {"path": path, "additions": 0, "deletions": 0, "patch": ""}
        for edit in edits[:200]:
            if not isinstance(edit, dict):
                continue
            piece = unified(
                path, str(edit.get("old_string") or ""), str(edit.get("new_string") or "")
            )
            merged["additions"] += piece["additions"]
            merged["deletions"] += piece["deletions"]
            merged["patch"] += piece["patch"]
        return merged
    return None
