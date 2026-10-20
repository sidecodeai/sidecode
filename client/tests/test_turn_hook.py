"""Amendment A51: the turn hooks Claude Code runs, as the bare interpreter runs them."""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.channel import paths, pty, turnhook, versions, wire
from rc_client.channel.settings import STOP_TIMEOUT, TURN_TIMEOUT, settings, write_settings
from rc_client.channel.versions import runs_tool_batch_hooks
from rc_client.config import ensure_dirs

from .test_attach_channel import short_dir  # noqa: F401  (fixture)

SCRIPT = Path(turnhook.__file__)
CHANNEL_PROMPT = '<channel source="rc" origin="app" message_id="m-1">\nhello\n</channel>'


def payload(event: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "sess-1",
        "transcript_path": "/t/sess-1.jsonl",
        "cwd": "/repo",
        "hook_event_name": event,
        **extra,
    }


def test_the_hook_spells_the_frames_as_the_wire_does() -> None:
    assert (turnhook.TURN, turnhook.TURN_REPLY) == (wire.TURN, wire.TURN_REPLY)
    assert sorted(turnhook.EVENTS.values()) == sorted(wire.TURN_EVENTS)
    kinds = {turnhook.prompt_kind(text)["prompt_kind"] for text in ("/x", "a", CHANNEL_PROMPT)}
    assert kinds <= set(wire.PROMPT_KINDS)


def test_the_hook_imports_nothing_but_the_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import: the file runs on its own"
            imported.add(str(node.module).split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


@pytest.mark.parametrize(
    ("prompt", "kind"),
    [
        (CHANNEL_PROMPT, {"prompt_kind": "ours", "message_id": "m-1"}),
        ("/model", {"prompt_kind": "command"}),
        ("Another Claude session sent a message:\n<teammate-message>x", {"prompt_kind": "agent"}),
        ("<task-notification>\n<summary>done</summary>", {"prompt_kind": "agent"}),
        (
            'Another Claude session sent a message:\n<agent-message from="w">',
            {"prompt_kind": "agent"},
        ),
        (
            'Another Claude session sent a message:\n<cross-session-message from="w">',
            {"prompt_kind": "agent"},
        ),
        ("fix the build", {"prompt_kind": "person"}),
    ],
)
def test_a_prompt_is_told_apart_by_whose_it_is(prompt: str, kind: dict[str, str]) -> None:
    assert turnhook.prompt_kind(prompt) == kind


def without_deadline(frame: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if frame is None else {k: v for k, v in frame.items() if k != "deadline"}


def test_the_frame_says_whose_prompt_it_was_and_never_what_it_said() -> None:
    frame = turnhook.frame(payload("UserPromptSubmit", prompt="the secret plan"))
    assert without_deadline(frame) == {
        "type": "turn",
        "event": "prompt",
        "session_id": "sess-1",
        "prompt_kind": "person",
    }
    assert "secret" not in json.dumps(frame)


def test_the_frame_says_when_the_hook_stops_waiting() -> None:
    """Counted from the hook's own start, which is when the script was loaded."""
    frame = turnhook.frame(payload("Stop"))
    assert frame is not None
    expected = turnhook.WALL_STARTED + turnhook.BUDGETS["stop"]
    assert abs(float(str(frame["deadline"])) - expected) < 0.01


def test_frames_the_device_has_no_part_in_are_not_sent() -> None:
    assert turnhook.frame(payload("PreToolUse")) is None
    assert turnhook.frame({**payload("Stop"), "session_id": "no spaces allowed"}) is None
    for event in ("UserPromptSubmit", "PostToolBatch", "Stop"):
        assert turnhook.frame(payload(event, agent_id="a-sub")) is None, "a subagent's own"
    assert without_deadline(turnhook.frame(payload("PostToolBatch"))) == {
        "type": "turn",
        "event": "batch",
        "session_id": "sess-1",
    }


def test_only_a_batch_with_something_to_hand_over_prints() -> None:
    reply = {"type": "turn_reply", "context": "the words", "system_message": "the line"}
    assert turnhook.output("batch", reply) == {
        "hookSpecificOutput": {"hookEventName": "PostToolBatch", "additionalContext": "the words"},
        "systemMessage": "the line",
    }
    assert turnhook.output("batch", {"type": "turn_reply"}) is None
    assert turnhook.output("stop", reply) is None
    assert turnhook.output("batch", {"type": "answers", "context": "x"}) is None
    assert turnhook.output("batch", None) is None


class Daemon:
    """One connection's worth of the daemon: reads a frame, then answers or not."""

    def __init__(self, path: Path, reply: dict[str, Any] | None, delay: float = 0.0) -> None:
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(1)
        self.frames: list[dict[str, Any]] = []
        self.reply, self.delay = reply, delay
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            line = connection.makefile("rb").readline()
            self.frames.append(json.loads(line))
            time.sleep(self.delay)
            if self.reply is not None:
                connection.sendall(wire.encode(self.reply))

    def close(self) -> None:
        self.server.close()


def run_hook(
    socket_path: Path, data: dict[str, Any]
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Run the hook as the settings file does, on the bare interpreter."""
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), str(socket_path)],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return done, time.monotonic() - started


def test_a_batch_hands_the_model_what_the_daemon_gave_it(short_dir: Path) -> None:  # noqa: F811
    path = short_dir / "c.sock"
    daemon = Daemon(path, wire.turn_reply(context="steered words", system_message="phone line"))
    try:
        done, _ = run_hook(path, payload("PostToolBatch", tool_calls=[]))
        daemon.thread.join(timeout=2)
    finally:
        daemon.close()
    assert done.returncode == 0
    assert json.loads(done.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolBatch",
            "additionalContext": "steered words",
        },
        "systemMessage": "phone line",
    }
    assert [without_deadline(frame) for frame in daemon.frames] == [
        {"type": "turn", "event": "batch", "session_id": "sess-1"}
    ]


def test_a_stop_waits_for_the_daemon_and_prints_nothing(short_dir: Path) -> None:  # noqa: F811
    path = short_dir / "c.sock"
    daemon = Daemon(path, wire.turn_reply(), delay=0.3)
    try:
        done, took = run_hook(path, payload("Stop", stop_hook_active=False))
        daemon.thread.join(timeout=2)
    finally:
        daemon.close()
    assert (done.returncode, done.stdout) == (0, "")
    assert took >= 0.3
    assert daemon.frames[0]["event"] == "stop"


def test_a_daemon_that_does_not_answer_costs_no_more_than_the_budget(
    short_dir: Path,  # noqa: F811
) -> None:
    path = short_dir / "c.sock"
    daemon = Daemon(path, None, delay=3)
    try:
        done, took = run_hook(path, payload("PostToolBatch"))
    finally:
        daemon.close()
    assert (done.returncode, done.stdout) == (0, "")
    assert took < turnhook.BUDGETS["batch"] + 0.5


def test_no_daemon_at_all_is_silent_and_quick(short_dir: Path) -> None:  # noqa: F811
    done, took = run_hook(short_dir / "missing.sock", payload("UserPromptSubmit", prompt="hi"))
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert took < 1.0


def test_garbage_on_stdin_is_silent(short_dir: Path) -> None:  # noqa: F811
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), str(short_dir / "x.sock")],
        input="not json",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_the_settings_file_runs_it_for_three_events_on_the_bare_interpreter() -> None:
    hooks = settings()["hooks"]
    command = paths.turn_hook_command()
    assert command[1:3] == ["-I", "-S"]
    assert Path(command[3]) == SCRIPT
    assert command[4] == str(paths.socket_path())
    for event, timeout in (
        ("UserPromptSubmit", TURN_TIMEOUT),
        ("PostToolBatch", TURN_TIMEOUT),
        ("Stop", STOP_TIMEOUT),
    ):
        [entry] = hooks[event]
        assert "matcher" not in entry
        [hook] = entry["hooks"]
        assert hook["type"] == "command"
        assert hook["timeout"] == timeout
        assert hook["command"].endswith(str(paths.socket_path()))
        assert str(SCRIPT) in hook["command"]
    assert max(turnhook.BUDGETS.values()) < STOP_TIMEOUT
    assert turnhook.BUDGETS["batch"] < TURN_TIMEOUT


def test_a_cli_too_old_for_the_batch_hook_keeps_the_other_two() -> None:
    hooks = settings(tool_batch=False)["hooks"]
    assert "PostToolBatch" not in hooks
    assert {"UserPromptSubmit", "Stop", "SessionStart", "PermissionRequest"} <= set(hooks)


@pytest.mark.parametrize(
    ("version", "runs"),
    [
        ("2.1.291", True),
        ("2.1.303 (Claude Code)", True),
        ("2.2.0", True),
        ("3.0.1", True),
        ("2.1.290", False),
        ("1.9.999", False),
        ("", False),
        (None, False),
        ("unknown", False),
    ],
)
def test_the_batch_hook_is_for_claude_code_2_1_291_and_later(
    version: str | None, runs: bool
) -> None:
    assert runs_tool_batch_hooks(version) is runs


def test_an_interrupt_still_exits_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*_args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(turnhook, "ask", interrupted)
    monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(payload("PostToolBatch"))))
    assert turnhook.main(["turnhook.py", "/nowhere.sock"]) == 0


class _Stdin:
    def __init__(self, text: str) -> None:
        self.buffer = _Bytes(text.encode("utf-8"))


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, _limit: int) -> bytes:
        return self.data


def test_both_settings_files_are_written_and_differ_by_the_batch_hook() -> None:
    ensure_dirs()
    write_settings()
    full = json.loads(paths.settings_path().read_text())["hooks"]
    lean = json.loads(paths.no_batch_settings_path().read_text())["hooks"]
    assert set(full) - set(lean) == {"PostToolBatch"}
    assert lean["Stop"] == full["Stop"]


def fake_claude(directory: Path, version: str) -> Path:
    """An executable that answers `--version` and counts how often it was asked."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "claude"
    target.write_text(
        f'#!/bin/sh\necho asked >> "{directory}/asked"\necho "{version} (Claude Code)"\n',
        encoding="utf-8",
    )
    target.chmod(0o755)
    return target


def asked(directory: Path) -> int:
    path = directory / "asked"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_a_version_is_asked_once_per_build_of_the_cli(tmp_path: Path) -> None:
    binary = fake_claude(tmp_path / "bin", "2.1.303")
    remembered = tmp_path / "state" / versions.VERSIONS_NAME
    remembered.parent.mkdir()
    assert versions.binary_version(str(binary), remembered) == "2.1.303"
    assert versions.binary_version(str(binary), remembered) == "2.1.303"
    assert asked(tmp_path / "bin") == 1
    binary.touch()
    os.utime(binary, (time.time() + 5, time.time() + 5))
    assert versions.binary_version(str(binary), remembered) == "2.1.303"
    assert asked(tmp_path / "bin") == 2


def test_a_new_build_with_the_old_modification_time_is_asked_again(tmp_path: Path) -> None:
    """A package can give every build the same modification time (1985, for npm)."""
    remembered = tmp_path / versions.VERSIONS_NAME
    binary = fake_claude(tmp_path / "bin", "2.1.290")
    os.utime(binary, (499162800, 499162800))
    assert versions.binary_version(str(binary), remembered) == "2.1.290"
    replacement = fake_claude(tmp_path / "other", "2.1.303 with a longer banner")
    os.replace(replacement, binary)
    os.utime(binary, (499162800, 499162800))
    assert versions.binary_version(str(binary), remembered) == "2.1.303"


def test_a_failed_probe_is_kept_only_for_a_while(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remembered = tmp_path / versions.VERSIONS_NAME
    binary = tmp_path / "claude"
    binary.write_text('#!/bin/sh\necho asked >> "$0.asked"\nexit 1\n')
    binary.chmod(0o755)
    assert versions.binary_version(str(binary), remembered) is None
    assert versions.binary_version(str(binary), remembered) is None
    assert len(Path(f"{binary}.asked").read_text().splitlines()) == 1
    later = time.time() + versions.FAILED_PROBE_TTL + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert versions.binary_version(str(binary), remembered) is None
    assert len(Path(f"{binary}.asked").read_text().splitlines()) == 2


@pytest.mark.parametrize(("version", "runs"), [("2.1.303", True), ("2.1.290", False)])
def test_the_proxy_hands_each_cli_the_settings_file_it_can_load(
    tmp_path: Path, version: str, runs: bool
) -> None:
    binary = fake_claude(tmp_path / "bin", version)
    state = tmp_path / "state"
    state.mkdir()
    full = state / versions.SETTINGS_NAME
    lean = state / versions.NO_BATCH_SETTINGS_NAME
    full.write_text("{}")
    lean.write_text("{}")
    command = [str(binary), "--model", "haiku", "--settings", str(full)]
    chosen, tool_batch = pty.settings_for(command, str(full))
    assert tool_batch is runs
    assert chosen == (command if runs else [*command[:-1], str(lean)])


def test_the_proxy_leaves_a_callers_own_settings_alone(tmp_path: Path) -> None:
    """Known by the shim's whole path, never by the name, which the person's file may share."""
    binary = fake_claude(tmp_path / "bin", "2.1.290")
    mine = tmp_path / "mine" / versions.SETTINGS_NAME
    ours = tmp_path / "state" / versions.SETTINGS_NAME
    command = [str(binary), "--settings", str(mine)]
    assert pty.settings_for(command, str(ours)) == (command, False)
    assert pty.settings_for(command, None) == (command, False)
    assert asked(tmp_path / "bin") == 0


def test_the_shim_names_its_settings_file_before_the_cli() -> None:
    argv = ["--shim-settings", "/s/claude-settings.json", "--", "claude", "--shim-settings", "x"]
    assert pty._shim_settings(argv) == "/s/claude-settings.json"
    assert pty._command(argv) == ["claude", "--shim-settings", "x"]
    assert pty._shim_settings(["--", "claude"]) is None
