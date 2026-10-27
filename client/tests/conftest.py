"""Shared fixtures."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from rc_client import daemon as daemon_module
from rc_client.agents.claude import account as claude_account
from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.claude import transcripts as claude_transcripts
from rc_client.agents.codex import runtime as codex_runtime
from rc_client.agents.codex.daemon import setup as codex_setup
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.sessions import mirror as mirror_module
from tests import agent_guard

# Refuse to start the agents installed on this machine, in every test, for good.
agent_guard.install()


@pytest.fixture(autouse=True)
def grok_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Grok's state directory at a scratch path.

    `~/.grok/sessions` holds a real person's conversations, and mirroring walks
    it on every scan; a test must never adopt one of those.
    """
    home = tmp_path / "grok-home"
    monkeypatch.setattr(grok_runtime, "home", lambda: home)


@pytest.fixture(autouse=True)
def pi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point pi's state directory at a scratch path, so detection never reads a person's
    own `~/.pi/agent/settings.json`."""
    home = tmp_path / "pi-home"
    monkeypatch.setattr(pi_runtime, "home", lambda: home)


@pytest.fixture(autouse=True)
def agent_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Claude's and Codex's homes at scratch paths, and stub the Keychain.

    Account detection reads whatever credential each agent keeps (A33). No test
    may read a real person's: the homes move to `tmp_path`, and the Keychain
    reader is replaced outright so the suite never asks the login keychain for
    the `Claude Code-credentials` item.
    """
    monkeypatch.setattr(claude_runtime, "CLAUDE_HOME", tmp_path / "claude-home" / ".claude")
    monkeypatch.setattr(codex_runtime, "CODEX_HOME", tmp_path / "codex-home")
    # A key exported in the shell the suite runs from would otherwise make the
    # developer's own machine decide what detection reports.
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    async def no_keychain() -> str | None:
        return None

    monkeypatch.setattr(claude_account, "read_keychain", no_keychain)


@pytest.fixture(autouse=True)
def no_real_agents(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep every test away from the agents installed on this machine.

    Detection resolves an agent's executable from candidates that are absolute
    or fixed when the module is imported (`STANDALONE`, `SYSTEM_PREFIXES`,
    `/opt/homebrew/bin/...`), which neither `PATH` nor `HOME` moves, and then
    runs it. So each `resolve_binary` finds only what lives in a temporary
    directory, and `agent_guard`'s audit hook refuses to start anything else,
    whichever way a test reached it. An attempt fails the test at its end even
    when the code under test swallowed the error. `RC_REAL_AGENTS=1`, the
    opt-in of the smoke tests, leaves all of it off.
    """
    agent_guard.allow(tmp_path_factory.getbasetemp())
    agent_guard.VIOLATIONS.clear()
    if not agent_guard.opted_in():
        absent = tmp_path / "no-agents" / "bin" / "codex"
        monkeypatch.setattr(codex_runtime, "STANDALONE", absent)
        monkeypatch.setattr(codex_setup, "STANDALONE", absent)
        monkeypatch.setattr(pi_runtime, "SYSTEM_PREFIXES", ())
        for runtime in (claude_runtime, codex_runtime, grok_runtime, pi_runtime):
            monkeypatch.setattr(
                runtime, "resolve_binary", agent_guard.fakes_only(runtime.resolve_binary)
            )
        # The daemon and the mirror took the functions by name when they were imported.
        monkeypatch.setattr(daemon_module, "resolve_codex", codex_runtime.resolve_binary)
        monkeypatch.setattr(daemon_module, "resolve_grok", grok_runtime.resolve_binary)
        monkeypatch.setattr(mirror_module, "resolve_codex", codex_runtime.resolve_binary)
        monkeypatch.setattr(mirror_module, "resolve_grok", grok_runtime.resolve_binary)
    yield
    attempts = list(agent_guard.VIOLATIONS)
    agent_guard.VIOLATIONS.clear()
    assert not attempts, "\n".join(attempts)


@pytest.fixture(autouse=True)
def claude_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point Claude's transcript directory at a scratch path.

    `PROJECTS_DIR` is computed when the module is imported, so moving
    `CLAUDE_HOME` does not move it. A scan that lists transcripts, and a rename
    that appends to one (A55), would otherwise reach a person's own `~/.claude`.
    """
    projects = tmp_path / "claude-home" / ".claude" / "projects"
    monkeypatch.setattr(claude_runtime, "PROJECTS_DIR", projects)
    monkeypatch.setattr(claude_transcripts, "PROJECTS_DIR", projects)


@pytest.fixture(autouse=True)
def client_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "rc-client"
    monkeypatch.setenv("RC_CLIENT_HOME", str(home))
    # Point the shared Codex daemon at a path that cannot exist, so a test never
    # reaches the developer's own daemon and drives their real threads.
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(tmp_path / "no-codex-daemon.sock"))
    yield home


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A directory short enough for an AF_UNIX path, which caps at 104 bytes."""
    directory = tempfile.mkdtemp(prefix="rc-")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
