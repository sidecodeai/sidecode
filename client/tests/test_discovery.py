"""Agent discovery against fake executables on PATH, one plugin at a time."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.claude.plugin import detect as detect_claude
from rc_client.agents.codex import runtime as codex_runtime
from rc_client.agents.codex.plugin import detect as detect_codex
from rc_client.agents.registry import AGENT_IDS, DetectContext, detect_all
from rc_client.channel import paths
from rc_client.channel.shim import MARKER, real_claude


def fake_binary(directory: Path, name: str, version: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f'#!/bin/sh\necho "{name} {version}"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


async def test_claude_is_reported_unavailable_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RC_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    info = await detect_claude(DetectContext())
    assert info.available is False
    assert info.path is None
    assert info.version is None


async def test_claude_detection_reads_the_version_and_advertises_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_binary(tmp_path / "bin", "claude", "2.1.266 (Claude Code)")
    monkeypatch.setenv("RC_CLAUDE_BIN", str(binary))
    info = await detect_claude(DetectContext())
    assert info.available is True
    assert info.path == str(binary)
    assert info.version == "2.1.266"
    # Most capable first, and Claude Code resolves every one of these aliases.
    assert [choice.id for choice in info.models] == [
        "default",
        "fable",
        "opus",
        "sonnet",
        "haiku",
    ]
    assert [choice.label for choice in info.models][:2] == ["Default", "Fable"]
    assert info.default_model == "default"
    assert info.speeds == []
    assert info.default_effort is None
    assert [choice.id for choice in info.permission_modes] == [
        "default",
        "acceptEdits",
        "plan",
        "bypassPermissions",
    ]
    assert set(info.capabilities) == {
        "takeover",
        "interrupt",
        "queue",
        "attachments",
        "effort",
        "history",
        "worktree",
        # A40: `/compact`, and nothing else Claude Code offers.
        "commands",
    }


async def test_explicit_override_wins_over_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_path = fake_binary(tmp_path / "path", "claude", "1.0.0")
    override = fake_binary(tmp_path / "override", "claude", "9.9.9")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("RC_CLAUDE_BIN", str(override))
    assert claude_runtime.resolve_binary() == str(override)
    monkeypatch.delenv("RC_CLAUDE_BIN")
    assert claude_runtime.resolve_binary() == str(on_path)


async def test_the_real_claude_behind_the_shim_on_path_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The service environment puts the shim directory first on PATH; the
    # daemon must step past the shim to the executable behind it.
    shim = fake_binary(tmp_path / "shim", "claude", "0.0.0")
    shim.write_text(f'#!/bin/sh\n# {MARKER}\nexec claude "$@"\n', encoding="utf-8")
    real = fake_binary(tmp_path / "real", "claude", "2.1.270")
    monkeypatch.setenv("PATH", os.pathsep.join([str(shim.parent), str(real.parent)]))
    monkeypatch.delenv("RC_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert claude_runtime.resolve_binary() == str(real)
    info = await detect_claude(DetectContext())
    assert info.available is True
    assert info.version == "2.1.270"


async def test_the_shim_directory_is_skipped_whatever_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One PATH walk serves both the shim and the daemon, and it steps over the
    # directory the shim lives in: a `claude` there belongs to this device even
    # when its contents do not say so, so the content check never has to decide.
    fake_binary(paths.bin_dir(), "claude", "0.0.0")
    real = fake_binary(tmp_path / "real", "claude", "2.1.270")
    monkeypatch.setenv("PATH", os.pathsep.join([str(paths.bin_dir()), str(real.parent)]))
    monkeypatch.delenv("RC_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert claude_runtime.resolve_binary() == str(real)
    assert real_claude() == str(real)


async def test_codex_detection_without_a_reachable_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_binary(tmp_path / "bin", "codex", "0.153.4")
    monkeypatch.setenv("RC_CODEX_BIN", str(binary))
    monkeypatch.setattr(
        "rc_client.agents.codex.plugin.catalog_cache.get",
        _fail_catalog,
    )
    info = await detect_codex(DetectContext())
    assert info.available is True
    assert info.version == "0.153.4"
    assert [choice.id for choice in info.permission_modes] == [
        "untrusted",
        "on-request",
        "never",
    ]
    assert info.default_permission_mode == "on-request"
    assert set(info.capabilities) == {
        "interrupt",
        "queue",
        "steer",
        "history",
        "worktree",
        "attachments",
        "effort",
        "commands",
    }


async def _fail_catalog(binary: str) -> object:
    from rc_client.agents.codex.models import ModelCatalog

    return ModelCatalog()


async def test_detect_all_returns_every_agent_in_registry_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("rc_client.agents.codex.plugin.catalog_cache.get", _fail_catalog)
    monkeypatch.delenv("RC_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("RC_CODEX_BIN", raising=False)
    agents = await detect_all()
    assert [info.agent for info in agents] == list(AGENT_IDS)
    # Absolute fallback locations are not controlled by PATH, so only assert on
    # the agent this test can actually hide.
    assert agents[0].available is False
    assert all(info.capabilities for info in agents)


def test_codex_candidate_list_covers_the_standard_install_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("RC_CODEX_BIN", raising=False)
    candidates = codex_runtime.candidate_paths()
    assert str(tmp_path / ".local/bin/codex") in candidates
    assert "/opt/homebrew/bin/codex" in candidates


async def test_version_probe_survives_a_binary_that_hangs_or_fails(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    assert await claude_runtime.probe_version(str(broken)) is None
    assert await claude_runtime.probe_version(str(tmp_path / "missing")) is None
    assert os.path.exists(broken)
