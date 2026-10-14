"""Shared fixtures."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from rc_client.agents.claude import account as claude_account
from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.codex import runtime as codex_runtime
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.pi import runtime as pi_runtime


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
