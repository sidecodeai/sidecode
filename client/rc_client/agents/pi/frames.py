"""The frames the pi extension and the device exchange over `pi-extension.sock`.

JSONL, LF only, one object per line, in both directions — the same framing rule
pi's own RPC mode uses, for the same reason: U+2028 and U+2029 are valid inside
JSON strings and must not be read as record separators.

Both ends of this are ours, so the vocabulary is small and closed. It is written
down in `docs/CLIENT.md` § "pi" as well.
"""

from __future__ import annotations

import json
from typing import Any

# Extension to device.
HELLO = "hello"
EVENT = "event"
INPUT = "input"
ASK = "ask"
ASK_CLOSED = "ask_closed"
REPLY = "reply"
BYE = "bye"

# Device to extension.
WELCOME = "welcome"
COMMAND = "command"
ANSWER = "answer"

# The commands a device sends. `send`, `abort`, `set_model`, `set_thinking` and
# `set_permission_mode` are the session controls; `stats` is what a turn's
# totals come from, because the socket has no `get_session_stats` of its own;
# `commands` and `compact` are amendment A27's two halves, the list a session
# offers and the one command of pi's own that the device runs by name.
SEND = "send"
ABORT = "abort"
SET_MODEL = "set_model"
SET_THINKING = "set_thinking"
SET_PERMISSION_MODE = "set_permission_mode"
STATS = "stats"
COMMANDS = "commands"
COMPACT = "compact"

# The pi run modes a `hello` can report.
TUI = "tui"
RPC = "rpc"

MAX_FRAME_BYTES = 32 * 1024 * 1024


def encode(frame: dict[str, Any]) -> bytes:
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any] | None:
    """One frame, or None when the line is not a JSON object."""
    try:
        message = json.loads(line.rstrip(b"\r\n"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return message if isinstance(message, dict) else None
