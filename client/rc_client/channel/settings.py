"""The `--settings` file the shim hands to Claude Code.

It carries two hooks. A `SessionStart` hook tells the daemon which session the
terminal is in now: the environment a CLI starts with is the environment its
MCP servers keep for as long as they live, so the channel bridge reports the
session id Claude Code picked at startup and nothing else, and `/resume` and
`/clear` move the conversation without the bridge ever hearing about it. The
hook fires on each of those, which is what keeps the attachment pointed at the
session the person is actually typing into.

A `PermissionRequest` hook on `AskUserQuestion` raises the CLI's own question
with the daemon (amendment A20). Claude Code races the hook against the dialog
it shows in the terminal and takes whichever answers first, so the hook waits
as long as the dialog does — a day, which is far longer than anyone leaves a
question open and short enough that nothing waits for ever.

Written by both `rc-client shim install` and the daemon at startup, like the MCP
config beside it, so the file matches whichever of the two ran most recently.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ..config import client_home, write_atomic
from . import paths

# Claude Code blocks on the session-start hook, so its budget is small: it only
# resolves a pid and writes one line to a local socket.
START_TIMEOUT = 5
# The question hook holds a person's question open, which is a different kind of
# wait entirely: it lives until the question is answered somewhere.
QUESTION_TIMEOUT = 24 * 60 * 60


def hook_line(command: list[str] | None = None) -> str:
    """The shell line Claude Code runs, with the device home pinned to this install."""
    argv = command or paths.hook_command()
    home = shlex.quote(str(client_home()))
    return f"RC_CLIENT_HOME={home} {shlex.join(argv)}"


def _entry(command: list[str] | None, timeout: int) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": hook_line(command), "timeout": timeout}]}


def settings(
    command: list[str] | None = None, question_command: list[str] | None = None
) -> dict[str, Any]:
    """The two hooks: one for every session start, one for `AskUserQuestion`."""
    return {
        "hooks": {
            "SessionStart": [_entry(command, START_TIMEOUT)],
            "PermissionRequest": [
                {
                    "matcher": "AskUserQuestion",
                    **_entry(question_command or paths.question_hook_command(), QUESTION_TIMEOUT),
                }
            ],
        }
    }


def write_settings(
    command: list[str] | None = None, question_command: list[str] | None = None
) -> Path:
    target = paths.settings_path()
    payload = json.dumps(settings(command, question_command), indent=2) + "\n"
    write_atomic(target, payload.encode("utf-8"), mode=0o600)
    return target
