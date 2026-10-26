"""`ExitPlanMode` in both directions: the plan block and Claude Code's verdicts (A54).

Two paths reach this module, as they do for `AskUserQuestion`. A session the
device drives sees the tool through the SDK's permission callback; a session a
terminal owns sees it through the `PermissionRequest` hook, because Claude Code
keeps a tool that needs the person off the channel's permission relay. Both
raise the same `approval` block marked `approval_kind: "plan"`, carrying the
plan once in `input.plan`, and both have to answer the way Claude Code's own
plan dialog does: approving sets the permission mode for the session, keeping
planning refuses the call with what the person wants changed.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from ...events import truncate_text

PLAN_TOOL = "ExitPlanMode"
APPROVAL_KIND = "plan"

ACCEPT_EDITS = "plan_accept_edits"
MANUAL = "plan_manual"
SHORTEN = "plan_shorten"
KEEP = "plan_keep"

# The plan travels in one event frame, which the gateway drops above 64 KiB, so
# the plan's own JSON encoding keeps well under that. Claude Code itself
# withholds approval of a plan past 200 000 characters.
MAX_PLAN_JSON_BYTES = 48 * 1024
MAX_PLAN_FILE_BYTES = 1024 * 1024
MAX_FEEDBACK_CHARS = 4000
TITLE_CHARS = 80

ACCEPT_MODE = "acceptEdits"
MANUAL_MODE = "default"

KEEP_MESSAGE = "The user rejected this plan and wants these changes: {feedback}"
SHORTEN_MESSAGE = (
    "The plan is too long for the user's phone to show in full, so it was not approved. "
    "Revise it to be much shorter and present it again."
)
DEFAULT_KEEP_MESSAGE = (
    "The user wants to keep planning. Stay in plan mode and wait for their reply."
)

_MARKDOWN_MARK = re.compile(r"^[\s#>*\-+`]+")


def _json_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def fit_plan(plan: str) -> tuple[str, bool]:
    """The plan as the wire carries it, and whether it had to be cut."""
    if _json_bytes(plan) <= MAX_PLAN_JSON_BYTES:
        return plan, False
    limit = MAX_PLAN_JSON_BYTES - 64
    cut, _ = truncate_text(plan, limit)
    while _json_bytes(cut) > MAX_PLAN_JSON_BYTES and limit > 256:
        limit = limit * 9 // 10
        cut, _ = truncate_text(plan, limit)
    return cut, True


def plan_title(plan: str) -> str:
    """`Plan ready: ` and the plan's first line, without its Markdown marks."""
    for line in plan.splitlines():
        text = _MARKDOWN_MARK.sub("", line).strip()
        if text:
            return f"Plan ready: {text[:TITLE_CHARS]}"
    return "Plan ready"


def plan_options(truncated: bool) -> list[dict[str, str]]:
    """The three choices, or what remains of them for a plan the phone cannot show whole."""
    keep = {"id": KEEP, "label": "No, keep planning", "style": "danger"}
    if truncated:
        return [{"id": SHORTEN, "label": "Ask for a shorter plan", "style": "primary"}, keep]
    return [
        {"id": ACCEPT_EDITS, "label": "Yes, auto-accept edits", "style": "primary"},
        {"id": MANUAL, "label": "Yes, manually approve edits", "style": "secondary"},
        keep,
    ]


def plan_fields(plan: str, truncated: bool) -> dict[str, Any]:
    """The `approval` block fields that make a plan card; the caller adds the ids."""
    fields: dict[str, Any] = {
        "approval_kind": APPROVAL_KIND,
        "title": plan_title(plan),
        "input": {"plan": plan},
        "options": plan_options(truncated),
    }
    if truncated:
        fields["plan_truncated"] = True
    return fields


def plan_text(tool_input: dict[str, Any]) -> str | None:
    """The plan the tool was called with, if Claude Code put it in the input."""
    plan = tool_input.get("plan")
    return plan if isinstance(plan, str) and plan.strip() else None


def plans_dir() -> Path:
    """Where Claude Code keeps its plan files, following `CLAUDE_CONFIG_DIR`."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(configured) if configured else Path.home() / ".claude") / "plans"


def plan_from_file(tool_input: dict[str, Any]) -> str | None:
    """The plan file the input names, only when it is a Markdown file under the plans directory.

    The file is opened without following a link or waiting on a pipe and judged
    by its descriptor, so what is read is what was checked. One past the size
    cap is not a plan.
    """
    named = tool_input.get("planFilePath")
    if not isinstance(named, str) or not named:
        return None
    try:
        path = Path(named).resolve(strict=True)
        root = plans_dir().resolve(strict=True)
        if root not in path.parents or path.suffix != ".md":
            return None
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PLAN_FILE_BYTES:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PLAN_FILE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > MAX_PLAN_FILE_BYTES:
        return None
    text = raw.decode("utf-8", "ignore")
    return text if text.strip() else None


def verdict(option_id: str, message: str | None, truncated: bool) -> dict[str, Any]:
    """What an app's choice means to Claude Code.

    Approving carries the permission mode the session continues in. A refusal
    carries what Claude is told; one with nothing to say interrupts the turn, as
    Escape does in the terminal's own dialog. An id the block never offered is
    read as keeping on planning, never as consent.
    """
    if option_id not in {option["id"] for option in plan_options(truncated)}:
        option_id = KEEP
    if option_id == ACCEPT_EDITS:
        return {"behavior": "allow", "mode": ACCEPT_MODE}
    if option_id == MANUAL:
        return {"behavior": "allow", "mode": MANUAL_MODE}
    if option_id == SHORTEN:
        return {"behavior": "deny", "message": SHORTEN_MESSAGE, "interrupt": False}
    feedback = (message or "").strip()[:MAX_FEEDBACK_CHARS]
    if feedback:
        return {
            "behavior": "deny",
            "message": KEEP_MESSAGE.format(feedback=feedback),
            "interrupt": False,
        }
    return {"behavior": "deny", "message": DEFAULT_KEEP_MESSAGE, "interrupt": True}
