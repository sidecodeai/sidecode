"""The newline-delimited JSON protocol between a channel bridge and the daemon.

One JSON object per line, UTF-8, no embedded newlines. Bridges are short-lived
and untrusted only in the sense that a stale one may still be connected, so the
reader caps the line length instead of growing without bound.
"""

from __future__ import annotations

import json
from typing import Any

MAX_LINE_BYTES = 256 * 1024

# bridge -> daemon
REGISTER = "register"
PERMISSION_REQUEST = "permission_request"
CLOSED = "closed"
# session-start hook -> daemon: one frame on its own connection, then EOF
SESSION_START = "session_start"
SESSION_START_SOURCES = ("startup", "resume", "clear", "compact")
# permission-request hook -> daemon: one frame, then the hook waits for `ANSWERS`
QUESTION = "question"
# pty proxy -> daemon: one frame naming the CLI it wraps, then request/response
PTY = "pty"
# daemon -> bridge
INJECT = "inject"
PERMISSION = "permission"
REGISTERED = "registered"
ANSWERS = "answers"
# daemon -> pty proxy, each answered with the same `id` (amendment A40)
KEYS = "keys"
SCREEN = "screen"
STATE = "state"
# pty proxy -> daemon: the reply to `KEYS`; `SCREEN` and `STATE` answer in kind
TYPED = "typed"


def encode(message: dict[str, Any]) -> bytes:
    """One frame: compact JSON followed by a newline."""
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any] | None:
    """Parse one frame, returning None for blank or malformed lines."""
    text = line.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def register(session_id: str, cwd: str, pid: int, claude_version: str | None) -> dict[str, Any]:
    return {
        "type": REGISTER,
        "session_id": session_id,
        "cwd": cwd,
        "pid": pid,
        "claude_version": claude_version,
    }


def session_start(
    session_id: str, cwd: str, pid: int, source: str, transcript_path: str
) -> dict[str, Any]:
    """What the `SessionStart` hook tells the daemon: which session `pid` serves now.

    `pid` is the Claude Code process itself (the hook's ancestor), never the
    hook's own shell. `source` is Claude Code's word for why the hook fired.
    """
    return {
        "type": SESSION_START,
        "session_id": session_id,
        "cwd": cwd,
        "pid": pid,
        "source": source,
        "transcript_path": transcript_path,
    }


def question(session_id: str, cwd: str, tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """What the `PermissionRequest` hook asks the daemon: raise this question.

    The hook then waits on the same connection for one `answers` frame, which
    is the only reply the daemon ever sends to a hook.
    """
    return {
        "type": QUESTION,
        "session_id": session_id,
        "cwd": cwd,
        "tool": tool,
        "input": tool_input,
    }


def answers(values: dict[str, Any] | None) -> dict[str, Any]:
    """The answer to a hook's question, or `None` when nobody remote gave one."""
    return {"type": ANSWERS, "answers": values}


def permission_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": PERMISSION_REQUEST,
        "request_id": str(payload.get("request_id") or ""),
        "tool_name": str(payload.get("tool_name") or ""),
        "description": str(payload.get("description") or ""),
        "input_preview": str(payload.get("input_preview") or ""),
    }


def closed() -> dict[str, Any]:
    return {"type": CLOSED}


def inject(message_id: str, text: str) -> dict[str, Any]:
    return {"type": INJECT, "message_id": message_id, "text": text}


def permission(request_id: str, behavior: str) -> dict[str, Any]:
    return {"type": PERMISSION, "request_id": request_id, "behavior": behavior}


def pty_register(pid: int) -> dict[str, Any]:
    """What the pseudo-terminal proxy tells the daemon: the CLI it wraps (A40).

    The pid is the Claude Code process itself, which is also what the channel
    bridge registers with (`os.getppid()`), so the two meet on one session.
    """
    return {"type": PTY, "pid": pid}


def keys(request_id: str, data: str, clear: bool = False) -> dict[str, Any]:
    """Type these characters into the terminal, exactly as a person would.

    `clear` forgets the screen first, so what a command draws can be read
    without the frames that came before it getting in the way.
    """
    frame: dict[str, Any] = {"type": KEYS, "id": request_id, "data": data}
    if clear:
        frame["clear"] = True
    return frame


def screen_request(request_id: str) -> dict[str, Any]:
    return {"type": SCREEN, "id": request_id}


def state_request(request_id: str) -> dict[str, Any]:
    return {"type": STATE, "id": request_id}


def typed(request_id: str, draft: int, idle_for: float) -> dict[str, Any]:
    """The reply to `keys`, carrying what the person is in the middle of doing.

    `draft` counts the characters they have typed since their last Enter,
    Escape, Ctrl-C or Ctrl-U; the text itself never leaves the terminal.
    """
    return {"type": TYPED, "id": request_id, "draft": draft, "idle_for": round(idle_for, 3)}


def screen(request_id: str, text: str, draft: int, idle_for: float) -> dict[str, Any]:
    return {
        "type": SCREEN,
        "id": request_id,
        "text": text,
        "draft": draft,
        "idle_for": round(idle_for, 3),
    }


def state(request_id: str, draft: int, idle_for: float) -> dict[str, Any]:
    return {"type": STATE, "id": request_id, "draft": draft, "idle_for": round(idle_for, 3)}
