"""The Claude Code hooks that say where a turn stands (amendment A51).

Claude Code runs this file for `UserPromptSubmit`, `PostToolBatch`, `Stop`,
`SubagentStart`, `SubagentStop` and `StopFailure`: on every prompt, every batch
of tool calls, every turn's end and every subagent's start and end, inside the
person's own session. So it is run by path with `python -I -S`, imports the standard library
and nothing else — not even this package — and is done well inside a tenth of
a second whenever the device has nothing for it.

- `UserPromptSubmit` tells the daemon a prompt went in and whose it was — the
  device's own injection by its message id, another agent's message, a slash
  command, or the person's words — never what it said.
- `PostToolBatch` asks for the messages a phone steered into the running turn
  and hands them to the model as the batch's additional context, with a line
  the terminal shows.
- `Stop` says the turn is ending, and how many background subagents and
  workflows it leaves running, and how many agents of any kind its task list
  holds — numbers, never what they run — and waits while the daemon writes
  what it held for this moment into the channel: up to two seconds, and only
  when it holds something, since the daemon answers at once otherwise.
- `SubagentStart` and `SubagentStop` name the subagent by its id, which is how
  the daemon knows one is at work; what it was asked never leaves the hook. A
  subagent that an API error ends fires `StopFailure` and no `SubagentStop`, so
  that is its end as well; the main thread's own `StopFailure` has no
  `agent_id` and says nothing to the device.

Every other hook of a subagent's or a teammate's carries an `agent_id` and is
none of the device's business: the person's messages go to the main thread.

Every failure is silent and the exit code is always 0: a hook of the device's
must never come between the person and Claude Code. The frame names below are
`rc_client.channel.wire`'s; a test keeps the two the same.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sys
import time
from collections.abc import Mapping

STARTED = time.monotonic()
WALL_STARTED = time.time()

TURN = "turn"
TURN_REPLY = "turn_reply"
# Claude Code's event names, and the device's for them.
EVENTS = {
    "UserPromptSubmit": "prompt",
    "PostToolBatch": "batch",
    "Stop": "stop",
    "SubagentStart": "agent_start",
    "SubagentStop": "agent_stop",
    "StopFailure": "agent_stop",
}
# The events that are about the subagent named in the payload's `agent_id`,
# which every other hook of a subagent's leaves to it.
AGENT_EVENTS = frozenset({"agent_start", "agent_stop"})
# How long each hook may take in all, counted from its start.
BUDGETS = {
    "prompt": 1.0,
    "batch": 1.0,
    "stop": 2.0,
    "agent_start": 1.0,
    "agent_stop": 1.0,
}

MAX_STDIN_BYTES = 8 * 1024 * 1024
MAX_REPLY_BYTES = 1024 * 1024
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
AGENT_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,128}\Z")
CHANNEL_HEAD = '<channel source="rc"'
MESSAGE_ID = re.compile(r'message_id="([^"]{1,64})"')
AGENT_MARKS = (
    "<teammate-message",
    "<agent-message",
    "<cross-session-message",
    "<task-notification",
)
# The `type`s of the `background_tasks` entries a `Stop` payload lists that are
# work the person still waits for. Every other type is left out on purpose: a
# shell may be a dev server or a watcher that never ends, and would hold the
# session for good; a teammate reads `running` for as long as it exists,
# finished or not; and Claude Code's own background work (dreams, memory
# imports, scans) is not the person's. A scheduled prompt (`session_crons`) is
# not work under way either.
BACKGROUND_KINDS = frozenset({"subagent", "workflow"})
# Every `type` that is an agent at work or waiting for work, a teammate included.
# A `Stop` whose list holds none of them leaves no agent running at all, which is
# how the daemon learns that a subagent whose end it never heard is gone.
AGENT_KINDS = BACKGROUND_KINDS | {"teammate"}
# The `status`es of such an entry that are work under way. Claude Code lists only
# running and pending work, so an entry saying anything else is finished or
# killed and nothing to hold a session for; one that says nothing is taken as
# under way.
LIVE_STATUSES = frozenset({"running", "pending"})


def prompt_kind(prompt: str) -> dict[str, str]:
    """Whose prompt this is, as far as the device needs to know; never its words."""
    head = prompt.lstrip()
    if head.startswith(CHANNEL_HEAD):
        found = MESSAGE_ID.search(head[:400])
        return {"prompt_kind": "ours", "message_id": found.group(1) if found else ""}
    if head.startswith("/"):
        return {"prompt_kind": "command"}
    if any(mark in prompt for mark in AGENT_MARKS):
        return {"prompt_kind": "agent"}
    return {"prompt_kind": "person"}


def under_way(task: object, kinds: frozenset[str] = BACKGROUND_KINDS) -> bool:
    """Whether a `background_tasks` entry is work of one of these kinds that is under way."""
    if not isinstance(task, dict):
        return False
    kind, status = task.get("type"), task.get("status")
    if not isinstance(kind, str) or kind not in kinds:
        return False
    return status is None or (isinstance(status, str) and status in LIVE_STATUSES)


def task_list(payload: Mapping[str, object]) -> list[object] | None:
    """The `Stop` payload's `background_tasks`, or None when it has no usable list."""
    tasks = payload.get("background_tasks")
    return tasks if isinstance(tasks, list) else None


def background_count(payload: Mapping[str, object]) -> int:
    """How many background subagents and workflows the turn leaves running, as a number.

    Claude Code lists the background tasks in the `Stop` payload's
    `background_tasks`, each with an id, a type, a status and a description of
    what it runs; only how many are under way leaves this process. A payload
    without the list, or with anything else in its place, as a Claude Code from
    before it has, leaves none.
    """
    tasks = task_list(payload)
    return 0 if tasks is None else sum(1 for task in tasks if under_way(task))


def agent_count(payload: Mapping[str, object]) -> int | None:
    """How many agents of any kind the list holds that are under way, or None.

    None says the payload has no usable list, which is not the same as holding
    none: a Claude Code from before the field has told the daemon nothing.
    """
    tasks = task_list(payload)
    return None if tasks is None else sum(1 for task in tasks if under_way(task, AGENT_KINDS))


def details(event: str, payload: Mapping[str, object]) -> Mapping[str, object] | None:
    """What a frame says beside its event, or None when the device has no part in the hook."""
    agent_id = str(payload.get("agent_id") or "")
    if event in AGENT_EVENTS:
        return {"agent_id": agent_id} if AGENT_ID.match(agent_id) else None
    if agent_id:
        return None
    if event == "prompt":
        return prompt_kind(str(payload.get("prompt") or ""))
    if event == "stop":
        counts: dict[str, object] = {"background": background_count(payload)}
        listed = agent_count(payload)
        if listed is not None:
            counts["listed_agents"] = listed
        return counts
    return {}


def frame(payload: Mapping[str, object]) -> dict[str, object] | None:
    """The frame for this hook's payload, or None when the device has no part in it.

    It carries the moment this hook stops waiting, in seconds since the epoch,
    so the daemon does not begin what it could not answer in time.
    """
    event = EVENTS.get(str(payload.get("hook_event_name") or ""))
    session_id = str(payload.get("session_id") or "")
    if event is None or not SESSION_ID.match(session_id):
        return None
    said = details(event, payload)
    if said is None:
        return None
    message: dict[str, object] = {"type": TURN, "event": event, "session_id": session_id, **said}
    message["deadline"] = round(WALL_STARTED + BUDGETS[event], 3)
    return message


def ask(path: str, message: Mapping[str, object], deadline: float) -> dict[str, object] | None:
    """One frame to the daemon, then its one-line reply, all before `deadline`."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(0.01, deadline - time.monotonic()))
        client.connect(path)
        client.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        buffer = bytearray()
        while len(buffer) <= MAX_REPLY_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            client.settimeout(remaining)
            chunk = client.recv(65536)
            if not chunk:
                return None
            buffer.extend(chunk)
            head, newline, _ = bytes(buffer).partition(b"\n")
            if newline:
                reply = json.loads(head)
                return reply if isinstance(reply, dict) else None
    return None


def output(event: str, reply: Mapping[str, object] | None) -> dict[str, object] | None:
    """What the hook prints: a batch's additional context, and nothing else ever."""
    if event != "batch" or reply is None or reply.get("type") != TURN_REPLY:
        return None
    context = reply.get("context")
    if not isinstance(context, str) or not context:
        return None
    result: dict[str, object] = {
        "hookSpecificOutput": {"hookEventName": "PostToolBatch", "additionalContext": context}
    }
    note = reply.get("system_message")
    if isinstance(note, str) and note:
        result["systemMessage"] = note
    return result


def _quit(_signum: int, _frame: object) -> None:
    """A hook Claude Code gave up on says nothing, like every other failure."""
    os._exit(0)


def main(argv: list[str]) -> int:
    """Always 0, whatever happens: an interrupt included, nothing here may stop Claude."""
    try:
        signal.signal(signal.SIGTERM, _quit)
        payload = json.loads(sys.stdin.buffer.read(MAX_STDIN_BYTES) or b"{}")
        message = frame(payload) if isinstance(payload, dict) else None
        if message is None or len(argv) < 2:
            return 0
        event = str(message["event"])
        reply = ask(argv[1], message, STARTED + BUDGETS[event])
        printed = output(event, reply)
        if printed is not None:
            sys.stdout.write(json.dumps(printed))
            sys.stdout.flush()
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
