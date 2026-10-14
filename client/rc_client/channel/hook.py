"""The Claude Code hooks this device installs, and what each one tells the daemon.

`hook session-start` says which session a terminal is in now: Claude Code runs
it on startup and again on `/resume`, `/clear` and a compaction. It writes one
`session_start` frame and leaves; the daemon answers nothing.

`hook permission-request` is the one Claude Code runs beside its own
`AskUserQuestion` dialog (amendment A20). It writes one `question` frame and
then waits, for hours if that is how long the person takes, for the daemon to
say whether an app answered first. Claude Code races the hook against its own
dialog and takes whichever resolves first, so printing a decision is how the
device answers the question from a phone, and printing nothing is how it steps
aside when the terminal got there first.

Two rules shape the whole module. **Anything a hook prints on stdout that is
not a decision is appended to the model's context**, so nothing else is ever
printed there. And a non-zero exit puts our stderr in front of the person, in
the middle of their own work, for a feature they did not ask about: every
failure here is silent and the exit code is always 0.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from typing import IO, Any

from ..agents.claude.questions import QUESTION_TOOL
from . import paths, wire

MAX_STDIN_BYTES = 64 * 1024
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")

SESSION_START = "session-start"
PERMISSION_REQUEST = "permission-request"
EVENTS = (SESSION_START, PERMISSION_REQUEST)

# One `ps` per ancestor, with a budget for the walk as a whole: Claude Code
# holds the session open while the hook runs.
PS_TIMEOUT = 1.0
ANCESTRY_BUDGET = 2.0
MAX_ANCESTRY_LEVELS = 6
SOCKET_TIMEOUT = 1.0

# (ppid, command line) of one process, or None when it cannot be read.
PsRunner = Callable[[int], tuple[int, str] | None]


def _ps(pid: int) -> tuple[int, str] | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=,command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head, _, command = result.stdout.strip().partition(" ")
    try:
        return int(head), command.strip()
    except ValueError:
        return None


def looks_like_claude(command: str) -> bool:
    """The same test the holder scan uses: the CLI itself, not one of its helpers."""
    for token in command.split()[:3]:
        base = os.path.basename(token)
        if base in {"claude", "claude-code"}:
            return True
        if base == "cli.js" and "claude" in token:
            return True
    return False


def claude_pid(runner: PsRunner | None = None) -> int:
    """The Claude Code process this hook belongs to.

    Today the hook's `/bin/sh` is a direct child of the CLI, so the parent is
    already the answer; walking up a few levels costs one `ps` each and keeps
    the answer right if a wrapper is ever inserted between the two.
    """
    ps = runner or _ps
    deadline = time.monotonic() + ANCESTRY_BUDGET
    parent = os.getppid()
    pid = parent
    for _ in range(MAX_ANCESTRY_LEVELS):
        if pid <= 1 or time.monotonic() >= deadline:
            break
        row = ps(pid)
        if row is None:
            break
        ppid, command = row
        if looks_like_claude(command):
            return pid
        pid = ppid
    return parent


def read_payload(stream: IO[bytes] | None = None) -> dict[str, Any] | None:
    """The single JSON object Claude Code writes on stdin, capped."""
    source = stream if stream is not None else sys.stdin.buffer
    try:
        raw = source.read(MAX_STDIN_BYTES)
    except (OSError, ValueError):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def frame(payload: dict[str, Any], pid: int) -> dict[str, Any] | None:
    """The `session_start` frame for this payload, or None when it says nothing usable."""
    session_id = str(payload.get("session_id") or "")
    if not SESSION_ID.match(session_id):
        return None
    source = str(payload.get("source") or "")
    return wire.session_start(
        session_id=session_id,
        cwd=str(payload.get("cwd") or ""),
        pid=pid,
        source=source if source in wire.SESSION_START_SOURCES else "startup",
        transcript_path=str(payload.get("transcript_path") or ""),
    )


def send(message: dict[str, Any]) -> None:
    """One frame to the daemon, then close. Nothing comes back."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(SOCKET_TIMEOUT)
        client.connect(str(paths.socket_path()))
        client.sendall(wire.encode(message))


def report() -> None:
    """Tell the daemon where this terminal is now, if the payload says."""
    payload = read_payload()
    if payload is None:
        return
    message = frame(payload, claude_pid())
    if message is not None:
        send(message)


# ------------------------------------------------------- the question the CLI asks


def question_frame(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The `question` frame for this payload, or None when it is not one to raise."""
    session_id = str(payload.get("session_id") or "")
    if not SESSION_ID.match(session_id):
        return None
    if str(payload.get("tool_name") or "") != QUESTION_TOOL:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    return wire.question(session_id, str(payload.get("cwd") or ""), QUESTION_TOOL, tool_input)


def decision(tool_input: dict[str, Any], answers: dict[str, str]) -> dict[str, Any]:
    """The hook output that answers the tool: an allow carrying the answers.

    Claude Code honours an allow for a tool that needs the person only when it
    comes with the input to run it with, which for `AskUserQuestion` means the
    questions it was called with plus what was chosen for each of them.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "allow",
                "updatedInput": {**tool_input, "answers": answers},
            },
        }
    }


def ask(message: dict[str, Any]) -> dict[str, Any] | None:
    """One frame to the daemon, then wait for the single reply it sends back.

    The wait has no deadline of its own. The same question is on screen in the
    terminal, and Claude Code ends this process when the answer arrives there,
    when the session goes away, or when the hook's own timeout runs out; the
    daemon hanging up reads as "nobody remote answered".
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(SOCKET_TIMEOUT)
        client.connect(str(paths.socket_path()))
        client.sendall(wire.encode(message))
        client.settimeout(None)
        return _reply(client)


def _reply(client: socket.socket) -> dict[str, Any] | None:
    """The first line the daemon sends, or None when it sends none."""
    buffer = bytearray()
    while len(buffer) <= wire.MAX_LINE_BYTES:
        chunk = client.recv(4096)
        if not chunk:
            return None
        buffer.extend(chunk)
        head, newline, _ = bytes(buffer).partition(b"\n")
        if newline:
            return wire.decode(head)
    return None


def relay_question() -> None:
    """Raise the question with the daemon and print whatever an app answered."""
    payload = read_payload()
    if payload is None:
        return
    message = question_frame(payload)
    if message is None:
        return
    reply = ask(message)
    answers = reply.get("answers") if reply is not None else None
    if not isinstance(answers, dict) or not answers:
        # Answered in the terminal, or nowhere at all: the CLI's dialog stands.
        return
    tool_input = message["input"]
    sys.stdout.write(json.dumps(decision(tool_input, answers)))


def _quit(_signum: int, _frame: Any) -> None:
    """A hook Claude Code gave up on says nothing, like every other failure."""
    raise SystemExit(0)


def main(event: str = SESSION_START) -> int:
    """Always 0: a failure here would interrupt the person's own session."""
    with contextlib.suppress(Exception):
        signal.signal(signal.SIGTERM, _quit)
        if event == PERMISSION_REQUEST:
            relay_question()
        else:
            report()
    return 0
