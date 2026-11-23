"""The device drives Antigravity as a thin client and nothing more (A64, 9.2).

It starts `agy acp` with no other argument, names itself `sidecode`, declares form questions and
nothing else, sends no `_meta`, no MCP server and no words but the person's, never calls
`authenticate`, never reads `agy`'s files or credentials and never calls Google itself. The suite's
own guard fails any test that touches `~/.gemini` (`tests/agent_guard.py`); these tests check the
rest, statically and while a whole session runs against the fake.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import runner as runner_module
from tests import agent_guard
from tests.agy_helpers import Recorder, entry_for, fake_agy, runner_for, wait_for

PACKAGE = Path(runner_module.__file__).parent
# Modules that would let the package reach the network, or a credential store, on its own.
# `urllib.parse` only parses a file URI.
FORBIDDEN_IMPORTS = frozenset(
    {
        "httpx",
        "urllib.request",
        "http",
        "socket",
        "ssl",
        "requests",
        "websockets",
        "keyring",
        "sqlite3",
    }
)
FORBIDDEN_WORDS = (".gemini", "keychain", "security", "authenticate", "googleapis", "cloudcode")
ALLOWED_METHODS = frozenset(
    {
        "initialize",
        "session/new",
        "session/resume",
        "session/load",
        "session/list",
        "session/prompt",
        "session/cancel",
        "session/set_config_option",
        "session/set_mode",
        "session/close",
    }
)

EVENTS: list[tuple[str, tuple[Any, ...]]] = []
RECORDING = [False]


def _record(event: str, args: tuple[Any, ...]) -> None:
    if RECORDING[0] and event in {
        "subprocess.Popen",
        "os.posix_spawn",
        "socket.connect",
        "socket.getaddrinfo",
    }:
        EVENTS.append((event, args))


sys.addaudithook(_record)


def sources() -> list[Path]:
    return sorted(PACKAGE.glob("*.py"))


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def test_the_package_imports_nothing_that_reaches_a_network_or_a_credential() -> None:
    for path in sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                parts = name.split(".")
                prefixes = {".".join(parts[: index + 1]) for index in range(len(parts))}
                assert not prefixes & FORBIDDEN_IMPORTS, f"{path.name} imports {name}"
                assert name != "urllib", f"{path.name} imports all of urllib"


def test_no_string_the_package_uses_names_agy_s_home_a_credential_or_google() -> None:
    for path in sources():
        tree = ast.parse(path.read_text())
        skipped = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skipped
            ):
                lowered = node.value.lower()
                for word in FORBIDDEN_WORDS:
                    assert word not in lowered, f"{path.name}: {node.value!r}"


async def test_a_whole_session_starts_only_agy_and_dials_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "WAKE_QUIET", 0.2)
    binary, peer = fake_agy(tmp_path, turns=["tool", "question", "steer", "wake"], wake_delay=0.2)
    recorder = Recorder()
    entry = entry_for(
        tmp_path, recorder, model="gemini-3.8-flash", effort="low", permission_mode="accept-edits"
    )
    runner = runner_for(entry, binary)
    EVENTS.clear()
    RECORDING[0] = True
    try:
        await runner.start()
        await runner.send(
            "List the files.", [{"name": "a.txt", "mime": "text/plain", "data_base64": "aGk="}]
        )
        await wait_for(lambda: recorder.events("approval"))
        await runner.approve(recorder.events("approval")[0]["request_id"], "allow", None)
        await wait_for(lambda: len(recorder.events("turn_completed")) == 1)
        await runner.send("Ask me.")
        await wait_for(lambda: recorder.events("question"))
        await runner.answer(recorder.events("question")[0]["request_id"], {"q1": ["1"]})
        await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
        await runner.send("Run sleep 8.")
        await wait_for(lambda: len(recorder.events("tool_call")) >= 3)
        await runner.steer("Also say STEERED.")
        await wait_for(lambda: len(recorder.events("turn_completed")) == 3)
        await runner.send("Background.")
        await wait_for(lambda: len(recorder.events("turn_completed")) == 5)
        await runner.shutdown()
    finally:
        RECORDING[0] = False

    assert not [event for event in EVENTS if event[0].startswith("socket.")], "nothing dialled"
    spawned = [
        os.path.basename(os.fsdecode(args[0]))
        for event, args in EVENTS
        if event in {"subprocess.Popen", "os.posix_spawn"}
    ]
    assert set(spawned) <= {"agy"}, spawned
    assert [start["argv"] for start in peer.starts()] == [["acp"]]

    sent = [message for message in peer.requests() if "method" in message]
    assert {message["method"] for message in sent} <= ALLOWED_METHODS
    assert all("_meta" not in str(message.get("params")) for message in sent), "no _meta, anywhere"
    for prompt in peer.calls("session/prompt"):
        texts = [block["text"] for block in prompt["prompt"] if block["type"] == "text"]
        assert texts in (
            ["List the files."],
            ["Ask me."],
            ["Run sleep 8."],
            ["Also say STEERED."],
            ["Background."],
        ), "the person's words alone"
    for opened in peer.calls("session/new"):
        assert opened["mcpServers"] == [] and set(opened) == {"cwd", "mcpServers"}
    assert not agent_guard.VIOLATIONS


def test_the_guard_refuses_a_touch_of_agy_s_home() -> None:
    """The hook refuses before anything is read; the path is one nobody has, all the same."""
    home = os.path.expanduser("~/.gemini/sidecode-guard-never-there")
    with pytest.raises(agent_guard.AgentHomeTouched):
        os.listdir(home)
    assert agent_guard.VIOLATIONS
    agent_guard.VIOLATIONS.clear()
