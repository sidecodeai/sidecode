"""The guard that keeps the suite from running the agents installed on this machine.

Every program started here is a harmless script of the test's own: it either
prints a word or touches a marker file, so a guard that failed would cost a
marker, never a real agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from rc_client import daemon as daemon_module
from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.codex import runtime as codex_runtime
from rc_client.agents.codex.daemon import setup as codex_setup
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.sessions import mirror as mirror_module
from tests import agent_guard

real_only = pytest.mark.skipif(
    agent_guard.opted_in(), reason="RC_REAL_AGENTS=1 turns the guard off on purpose"
)


def script(directory: Path, name: str, marker: Path | None = None) -> Path:
    """An executable that touches `marker` when it runs, or else prints a word."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body = f"touch '{marker}'" if marker is not None else "echo fake"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Narrow the directories a fake may live in, so `tmp_path/machine` is somebody's own."""
    monkeypatch.setattr(agent_guard, "ROOTS", [str(tmp_path / "fakes")])
    return tmp_path / "machine"


def attempts() -> list[str]:
    """The attempts the guard recorded, which the test then takes as its own."""
    seen = list(agent_guard.VIOLATIONS)
    agent_guard.VIOLATIONS.clear()
    return seen


# ------------------------------------------------------------------ the audit hook


@real_only
def test_a_fake_agent_in_a_temporary_directory_starts(tmp_path: Path) -> None:
    fake = script(tmp_path / "bin", "codex")

    done = subprocess.run([str(fake)], capture_output=True, text=True, check=True)

    assert done.stdout.strip() == "fake"
    assert attempts() == []


@real_only
def test_an_agent_outside_the_temporary_directories_is_refused_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    real = script(outside(tmp_path, monkeypatch) / "bin", "codex", marker)

    with pytest.raises(agent_guard.RealAgentStarted) as caught:
        subprocess.Popen([str(real), "--version"])

    assert str(real) in str(caught.value)
    assert not marker.exists()
    assert len(attempts()) == 1


@real_only
async def test_an_agent_started_through_asyncio_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    real = script(outside(tmp_path, monkeypatch) / "bin", "pi", marker)

    with pytest.raises(agent_guard.RealAgentStarted):
        await asyncio.create_subprocess_exec(str(real), "--version")
    await asyncio.sleep(0.05)

    assert not marker.exists()
    assert len(attempts()) == 1


@real_only
@pytest.mark.skipif(not hasattr(os, "posix_spawn"), reason="no posix_spawn here")
def test_an_agent_started_with_posix_spawn_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    real = script(outside(tmp_path, monkeypatch) / "bin", "claude", marker)

    with pytest.raises(agent_guard.RealAgentStarted):
        os.posix_spawn(str(real), [str(real)], dict(os.environ))

    assert not marker.exists()
    assert len(attempts()) == 1


@real_only
@pytest.mark.parametrize("name", sorted(agent_guard.AGENT_NAMES))
def test_every_agent_name_is_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    real = script(outside(tmp_path, monkeypatch) / "bin", name)

    with pytest.raises(agent_guard.RealAgentStarted):
        subprocess.Popen([str(real)])

    assert len(attempts()) == 1


@real_only
def test_an_agent_named_only_as_the_first_argument_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whichever of the program and its `argv[0]` names an agent."""
    real = script(outside(tmp_path, monkeypatch) / "bin", "grok")

    with pytest.raises(agent_guard.RealAgentStarted):
        subprocess.Popen([str(real), "-c", "exit 0"], executable="/bin/sh")

    assert len(attempts()) == 1


@real_only
def test_an_attempt_the_code_under_test_swallows_is_still_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = script(outside(tmp_path, monkeypatch) / "bin", "codex")

    with contextlib.suppress(Exception):
        subprocess.Popen([str(real)])

    assert len(attempts()) == 1


@real_only
def test_other_programs_are_none_of_the_guards_business(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = outside(tmp_path, monkeypatch)
    for name in ("git", "python3", "codex-notes", "pi.txt"):
        assert agent_guard.real_agent(str(script(machine / "bin", name)), roots=[]) is None
    done = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, check=True)
    assert done.stdout.strip() == b"1"
    assert attempts() == []


def test_a_program_that_is_not_there_starts_nothing_and_is_let_through(tmp_path: Path) -> None:
    assert agent_guard.real_agent(str(tmp_path / "nowhere" / "codex"), roots=[]) is None
    assert agent_guard.real_agent("codex", env={"PATH": str(tmp_path / "empty")}, roots=[]) is None


def test_a_bare_name_is_looked_for_where_the_child_would_look(
    tmp_path: Path,
) -> None:
    machine = script(tmp_path / "machine" / "bin", "pi")
    fakes = script(tmp_path / "fakes" / "bin", "pi")
    roots = [str(tmp_path / "fakes")]

    assert agent_guard.real_agent("pi", env={"PATH": str(machine.parent)}, roots=roots) == str(
        machine
    )
    assert agent_guard.real_agent("pi", env={"PATH": str(fakes.parent)}, roots=roots) is None
    assert agent_guard.real_agent("pi", env={"PATH": str(tmp_path / "none")}, roots=roots) is None


def test_a_path_under_a_root_is_inside_and_a_symlink_out_of_one_is_not(tmp_path: Path) -> None:
    fakes = tmp_path / "fakes"
    target = script(tmp_path / "machine" / "bin", "codex")
    fakes.mkdir()
    (fakes / "codex").symlink_to(target)
    roots = [str(fakes)]

    assert agent_guard.inside(fakes / "anything", roots)
    assert not agent_guard.inside(tmp_path / "fakes-and-more", roots)
    assert agent_guard.real_agent(str(fakes / "codex"), roots=roots) == str(target.resolve())
    assert agent_guard.real_agent(str(fakes / "codex"), roots=[]) == str(target.resolve())


def test_the_opt_in_turns_the_hook_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "ran"
    real = script(outside(tmp_path, monkeypatch) / "bin", "codex", marker)
    monkeypatch.setenv(agent_guard.OPT_IN, "1")

    subprocess.run([str(real)], check=True)

    assert marker.exists()
    assert attempts() == []


# ------------------------------------------------------------------ what the fixture changes


@real_only
def test_no_agent_on_this_machine_is_ever_found_by_a_plain_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in ("RC_CLAUDE_BIN", "RC_CODEX_BIN", "RC_GROK_BIN", "RC_PI_BIN"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert not codex_runtime.STANDALONE.exists()
    assert not vars(pi_runtime)["SYSTEM_PREFIXES"]
    for runtime in (claude_runtime, codex_runtime, grok_runtime, pi_runtime):
        assert runtime.resolve_binary() is None, runtime.__name__


@real_only
def test_the_daemon_and_the_mirror_use_the_same_filtered_resolvers() -> None:
    assert vars(daemon_module)["resolve_codex"] is codex_runtime.resolve_binary
    assert vars(daemon_module)["resolve_grok"] is grok_runtime.resolve_binary
    assert vars(mirror_module)["resolve_codex"] is codex_runtime.resolve_binary
    assert vars(mirror_module)["resolve_grok"] is grok_runtime.resolve_binary
    assert not vars(codex_setup)["STANDALONE"].exists()


@real_only
@pytest.mark.parametrize(
    ("runtime", "variable", "name"),
    [
        (claude_runtime, "RC_CLAUDE_BIN", "claude"),
        (codex_runtime, "RC_CODEX_BIN", "codex"),
        (grok_runtime, "RC_GROK_BIN", "grok"),
        (pi_runtime, "RC_PI_BIN", "pi"),
    ],
)
def test_an_explicit_override_is_found_only_where_a_fake_lives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: ModuleType,
    variable: str,
    name: str,
) -> None:
    fakes = script(tmp_path / "fakes" / "bin", name)
    machine = script(tmp_path / "machine" / "bin", name)
    monkeypatch.setattr(agent_guard, "ROOTS", [str(tmp_path / "fakes")])

    monkeypatch.setenv(variable, str(fakes))
    assert runtime.resolve_binary() == str(fakes)
    monkeypatch.setenv(variable, str(machine))
    assert runtime.resolve_binary() is None
