"""The `--settings` file the shim hands to Claude Code.

It carries up to eight hooks. A `SessionStart` hook tells the daemon which session the
terminal is in now: the environment a CLI starts with is the environment its
MCP servers keep for as long as they live, so the channel bridge reports the
session id Claude Code picked at startup and nothing else, and `/resume` and
`/clear` move the conversation without the bridge ever hearing about it. The
hook fires on each of those, which is what keeps the attachment pointed at the
session the person is actually typing into.

A `PermissionRequest` hook on `AskUserQuestion` and `ExitPlanMode` raises the
CLI's own question, or the plan it wants approved, with the daemon (amendments
A20 and A54). Claude Code races the hook against the dialog
it shows in the terminal and takes whichever answers first, so the hook waits
as long as the dialog does — a day, which is far longer than anyone leaves a
question open and short enough that nothing waits for ever.

`UserPromptSubmit`, `PostToolBatch`, `Stop`, `SubagentStart`, `SubagentStop` and
`StopFailure` run `turnhook.py`, which tells the daemon where each turn stands
the moment the CLI knows it, which subagents are at work, hands the model a
message a phone steered into the turn at the next batch of tool calls, and
holds a turn's end while the messages waiting for it go into the channel
(amendment A51). A Claude Code too old for `PostToolBatch` must not be handed
it, nor the subagent hooks and `StopFailure` beside it, which are older but may
not be known to a CLI whose version could not be read, so a second file carries
every hook but those, and the terminal proxy passes whichever the CLI it starts
can load (`versions.py`).

Both are written by `rc-client shim install` and by the daemon at startup, like
the MCP config beside them, so they match whichever of the two ran last.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ..agents.claude.plans import PLAN_TOOL
from ..agents.claude.questions import QUESTION_TOOL
from ..config import client_home, write_atomic
from . import paths

# Claude Code blocks on the session-start hook, so its budget is small: it only
# resolves a pid and writes one line to a local socket.
START_TIMEOUT = 5
# The question hook holds a person's question open, which is a different kind of
# wait entirely: it lives until the question is answered somewhere.
QUESTION_TIMEOUT = 24 * 60 * 60
# A51: the turn hooks keep to their own budgets (`turnhook.py`): a second, and
# two for `Stop` while it waits on held messages being written. These are
# Claude Code's backstop, a second beyond each.
TURN_TIMEOUT = 2
STOP_TIMEOUT = 3
TOOL_BATCH_EVENT = "PostToolBatch"
# The events only a CLI that can run `PostToolBatch` is given.
NEWER_EVENTS = frozenset({TOOL_BATCH_EVENT, "SubagentStart", "SubagentStop", "StopFailure"})
TURN_EVENTS = (
    ("UserPromptSubmit", TURN_TIMEOUT),
    (TOOL_BATCH_EVENT, TURN_TIMEOUT),
    ("SubagentStart", TURN_TIMEOUT),
    ("SubagentStop", TURN_TIMEOUT),
    ("StopFailure", TURN_TIMEOUT),
    ("Stop", STOP_TIMEOUT),
)


def hook_line(command: list[str] | None = None) -> str:
    """The shell line Claude Code runs, with the device home pinned to this install."""
    argv = command or paths.hook_command()
    home = shlex.quote(str(client_home()))
    return f"RC_CLIENT_HOME={home} {shlex.join(argv)}"


def _entry(command: list[str] | None, timeout: int) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": hook_line(command), "timeout": timeout}]}


def _turn_entry(command: list[str], timeout: int) -> dict[str, Any]:
    """A turn hook names its socket itself, so it needs no device home in front of it."""
    return {"hooks": [{"type": "command", "command": shlex.join(command), "timeout": timeout}]}


def settings(
    command: list[str] | None = None,
    question_command: list[str] | None = None,
    turn_command: list[str] | None = None,
    *,
    tool_batch: bool = True,
) -> dict[str, Any]:
    """Every session start, `AskUserQuestion`, `ExitPlanMode`, and where each turn stands (A51).

    `tool_batch` is whether the CLI runs `PostToolBatch` hooks at all, and so
    whether it is handed the subagent hooks and `StopFailure` that go with them.
    """
    turn = turn_command or paths.turn_hook_command()
    events = [
        (event, timeout)
        for event, timeout in TURN_EVENTS
        if tool_batch or event not in NEWER_EVENTS
    ]
    return {
        "hooks": {
            "SessionStart": [_entry(command, START_TIMEOUT)],
            "PermissionRequest": [
                {
                    "matcher": f"{QUESTION_TOOL}|{PLAN_TOOL}",
                    **_entry(question_command or paths.question_hook_command(), QUESTION_TIMEOUT),
                }
            ],
            **{event: [_turn_entry(turn, timeout)] for event, timeout in events},
        }
    }


def write_settings(
    command: list[str] | None = None,
    question_command: list[str] | None = None,
    turn_command: list[str] | None = None,
) -> Path:
    """Both settings files, with and without `PostToolBatch`; the first one's path."""
    for target, tool_batch in (
        (paths.settings_path(), True),
        (paths.no_batch_settings_path(), False),
    ):
        hooks = settings(command, question_command, turn_command, tool_batch=tool_batch)
        payload = json.dumps(hooks, indent=2) + "\n"
        write_atomic(target, payload.encode("utf-8"), mode=0o600)
    return paths.settings_path()
