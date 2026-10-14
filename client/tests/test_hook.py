"""The hooks: what each sends, what neither ever prints, and when they give up."""

from __future__ import annotations

import io
import json
import os
import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from rc_client.channel import hook, paths, wire
from rc_client.channel.settings import QUESTION_TIMEOUT, START_TIMEOUT, hook_line, settings

CLAUDE = "/Users/me/.local/bin/claude --dangerously-load-development-channels server:rc"
HOOK_SHELL = "/bin/sh -c RC_CLIENT_HOME=/home/me/.rc-client rc-client hook session-start"

PAYLOAD = {
    "session_id": "8dc12b5f-b14b-4c83-8792-f7c2dea77b6a",
    "transcript_path": "/Users/me/.claude/projects/-tmp-proj/8dc12b5f.jsonl",
    "cwd": "/tmp/proj",
    "hook_event_name": "SessionStart",
    "source": "resume",
    "seconds_since_last_response": 5,
}

QUESTION_INPUT = {
    "questions": [
        {
            "header": "Skew",
            "question": "How should the refresh window treat skew?",
            "options": [{"label": "Clamp it"}, {"label": "Go monotonic"}],
            "multiSelect": False,
        }
    ]
}
QUESTION_PAYLOAD = {
    "session_id": PAYLOAD["session_id"],
    "transcript_path": PAYLOAD["transcript_path"],
    "cwd": "/tmp/proj",
    "hook_event_name": "PermissionRequest",
    "tool_name": "AskUserQuestion",
    "tool_input": QUESTION_INPUT,
}
ANSWERS = {"How should the refresh window treat skew?": "Go monotonic"}


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """The daemon's end of the channel socket, without a daemon behind it.

    A connection sits in the backlog until it is accepted, so a hook that sends
    one frame and closes needs nothing listening concurrently.
    """
    path = paths.socket_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen(1)
        yield server
    finally:
        server.close()
        path.unlink(missing_ok=True)


def received(server: socket.socket, timeout: float = 1.0) -> dict[str, Any] | None:
    server.settimeout(timeout)
    try:
        connection, _ = server.accept()
    except OSError:
        return None
    with connection:
        return wire.decode(connection.recv(wire.MAX_LINE_BYTES))


def run_hook(payload: Any, monkeypatch: pytest.MonkeyPatch, event: str = hook.SESSION_START) -> int:
    """`main()` with `payload` on stdin and a pid resolution that needs no `ps`."""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    monkeypatch.setattr(hook, "claude_pid", lambda *args: 4242)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    return hook.main(event)


def serve_once(
    server: socket.socket, reply: dict[str, Any] | None
) -> tuple[threading.Thread, list[dict[str, Any] | None]]:
    """Answer one waiting hook from another thread; the hook blocks on `recv`."""
    seen: list[dict[str, Any] | None] = []

    def run() -> None:
        server.settimeout(2.0)
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            seen.append(wire.decode(connection.recv(wire.MAX_LINE_BYTES)))
            if reply is not None:
                connection.sendall(wire.encode(reply))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, seen


def test_the_hook_sends_one_frame_and_says_nothing(
    listener: socket.socket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_hook(PAYLOAD, monkeypatch) == 0
    assert received(listener) == {
        "type": "session_start",
        "session_id": PAYLOAD["session_id"],
        "cwd": "/tmp/proj",
        "pid": 4242,
        "source": "resume",
        "transcript_path": PAYLOAD["transcript_path"],
    }
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"",
        b"[1, 2]",
        {"source": "startup"},
        {"session_id": "", "source": "startup"},
        {"session_id": "has space", "source": "startup"},
        {"session_id": "x" * 81},
        {"session_id": "../../etc/passwd"},
    ],
    ids=["malformed", "empty", "not-an-object", "no-id", "blank-id", "space", "too-long", "path"],
)
def test_a_payload_that_names_no_session_sends_nothing(
    payload: Any,
    listener: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_hook(payload, monkeypatch) == 0
    assert received(listener, timeout=0.2) is None
    assert capsys.readouterr().out == ""


def test_an_unknown_source_is_read_as_a_startup(
    listener: socket.socket, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_hook({**PAYLOAD, "source": "teleport"}, monkeypatch) == 0
    frame = received(listener)
    assert frame is not None and frame["source"] == "startup"


def test_no_daemon_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is listening, so `connect` fails: the person must never see it."""
    assert not paths.socket_path().exists()
    assert run_hook(PAYLOAD, monkeypatch) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_the_claude_process_is_found_above_the_hooks_own_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getppid", lambda: 10)
    tree = {10: (11, HOOK_SHELL), 11: (12, CLAUDE), 12: (1, "-zsh")}

    def ps(pid: int) -> tuple[int, str] | None:
        return tree.get(pid)

    assert hook.claude_pid(ps) == 11


def test_the_parent_is_the_answer_when_nothing_above_looks_like_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "getppid", lambda: 10)
    tree = {10: (11, "/bin/sh /tmp/hook.sh"), 11: (1, "-zsh")}

    def ps(pid: int) -> tuple[int, str] | None:
        return tree.get(pid)

    assert hook.claude_pid(ps) == 10
    assert hook.claude_pid(lambda _pid: None) == 10


def test_the_walk_stops_before_a_cycle_or_a_deep_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getppid", lambda: 10)
    calls: list[int] = []

    def ps(pid: int) -> tuple[int, str] | None:
        calls.append(pid)
        return (pid + 1, "/usr/bin/python3 worker.py")

    assert hook.claude_pid(ps) == 10
    assert len(calls) == hook.MAX_ANCESTRY_LEVELS


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (CLAUDE, True),
        ("/Users/me/.local/bin/claude", True),
        ("node /Users/me/.claude/local/cli.js --resume abc", True),
        ("/bin/sh -c rc-client hook session-start", False),
        ("-zsh", False),
        ("", False),
    ],
)
def test_only_the_cli_itself_counts_as_claude(command: str, expected: bool) -> None:
    assert hook.looks_like_claude(command) is expected


def test_ps_reads_this_processs_real_parent() -> None:
    row = hook._ps(os.getpid())
    assert row is not None
    parent, command = row
    assert parent == os.getppid()
    assert command
    assert hook._ps(999_999) is None


def test_the_hook_line_quotes_the_home_and_the_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A home with a space in it has to stay one word, and the assignment one token."""
    monkeypatch.setenv("RC_CLIENT_HOME", "/Users/me/my home/.rc-client")
    assert hook_line(["/opt/rc bin/rc-client", "hook", "session-start"]) == (
        "RC_CLIENT_HOME='/Users/me/my home/.rc-client' '/opt/rc bin/rc-client' hook session-start"
    )


def test_the_hook_entry_is_one_command_with_a_timeout() -> None:
    entries = settings()["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert "matcher" not in entries[0]
    entry = entries[0]["hooks"][0]
    assert entry["type"] == "command"
    assert entry["timeout"] == START_TIMEOUT == 5
    assert entry["command"].endswith("hook session-start")


def test_the_question_hook_is_matched_to_the_tool_and_waits_a_day() -> None:
    entries = settings()["hooks"]["PermissionRequest"]
    assert len(entries) == 1
    assert entries[0]["matcher"] == "AskUserQuestion"
    entry = entries[0]["hooks"][0]
    assert entry["type"] == "command"
    assert entry["timeout"] == QUESTION_TIMEOUT == 86400
    assert entry["command"].endswith("hook permission-request")


# ------------------------------------------- the question hook (amendment A20)


def test_the_question_hook_prints_the_decision_an_app_answered(
    listener: socket.socket, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    thread, seen = serve_once(listener, wire.answers(ANSWERS))
    assert run_hook(QUESTION_PAYLOAD, monkeypatch, hook.PERMISSION_REQUEST) == 0
    thread.join(timeout=2)

    assert seen == [
        {
            "type": "question",
            "session_id": PAYLOAD["session_id"],
            "cwd": "/tmp/proj",
            "tool": "AskUserQuestion",
            "input": QUESTION_INPUT,
        }
    ]
    printed = json.loads(capsys.readouterr().out)
    output = printed["hookSpecificOutput"]
    assert output["hookEventName"] == "PermissionRequest"
    assert output["decision"]["behavior"] == "allow"
    assert output["decision"]["updatedInput"] == {**QUESTION_INPUT, "answers": ANSWERS}


@pytest.mark.parametrize(
    "reply",
    [wire.answers(None), {"type": "answers"}, None],
    ids=["answered-elsewhere", "nothing-to-say", "hung-up"],
)
def test_a_question_nobody_answered_remotely_prints_nothing(
    reply: dict[str, Any] | None,
    listener: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The terminal's own dialog stands, so the hook must not decide anything."""
    thread, _ = serve_once(listener, reply)
    assert run_hook(QUESTION_PAYLOAD, monkeypatch, hook.PERMISSION_REQUEST) == 0
    thread.join(timeout=2)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_a_question_hook_with_no_daemon_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not paths.socket_path().exists()
    assert run_hook(QUESTION_PAYLOAD, monkeypatch, hook.PERMISSION_REQUEST) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        {"tool_name": "AskUserQuestion", "tool_input": QUESTION_INPUT},
        {"session_id": "has space", "tool_name": "AskUserQuestion", "tool_input": QUESTION_INPUT},
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"session_id": "s1", "tool_name": "AskUserQuestion", "tool_input": "not an object"},
    ],
    ids=["malformed", "no-id", "bad-id", "another-tool", "no-input"],
)
def test_a_question_payload_this_device_cannot_raise_sends_nothing(
    payload: Any,
    listener: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_hook(payload, monkeypatch, hook.PERMISSION_REQUEST) == 0
    assert received(listener, timeout=0.2) is None
    assert capsys.readouterr().out == ""
