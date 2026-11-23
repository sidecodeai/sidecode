"""The generic touch points Antigravity uses outside its package (A64, A58).

The registry's optional service hook, which the daemon starts once the agent has started and stops
with itself; a plugin's own words for a refused message; and `own_process` reading a runner's
`private_process`, which the Claude and pi runners set too.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.agents.antigravity import plugin, presence
from rc_client.agents.antigravity import runner as runner_module
from rc_client.agents.antigravity.holding import Holding
from rc_client.agents.antigravity.presence import Holder, Holders, Place
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.agents.pi.adapter import PiRunner
from rc_client.daemon import Daemon
from rc_client.registry import Registry
from rc_client.sessions import idle
from rc_client.sessions.hub import SessionHub
from tests.agy_helpers import SESSION, agent_info, fake_agy, wait_for
from tests.test_daemon import running_daemon

__all__ = ["running_daemon"]


async def test_the_registry_finds_a_plugin_s_service_and_its_own_conflict_words() -> None:
    assert await registry.service_for("antigravity") is plugin.SERVICE
    assert await registry.service_for("claude") is None
    registry.plugin("antigravity")
    assert registry.terminal_conflict("antigravity") == plugin.TERMINAL_CONFLICT
    assert registry.terminal_conflict("codex") is None


def test_the_device_s_own_processes_say_so() -> None:
    assert ClaudeRunner.private_process is True
    assert PiRunner.private_process is True


async def test_the_daemon_starts_the_service_once_antigravity_has_started(
    running_daemon: tuple[Daemon, Any],
) -> None:
    daemon, _ = running_daemon
    await wait_for(lambda: plugin.SERVICE in daemon._services)
    assert plugin.SERVICE.hub is daemon.hub
    assert plugin.SERVICE._tasks and not any(task.done() for task in plugin.SERVICE._tasks)


async def test_a_process_nobody_uses_is_stopped_but_not_while_agy_works_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "WAKE_QUIET", 0.2)
    binary, _ = fake_agy(tmp_path, turns=["wake"], wake_delay=0.6)
    db = Registry(tmp_path / "state.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(db, publish, "dev-1", lambda: [agent_info(binary)])
    try:
        await hub.create({"agent": "antigravity", "cwd": str(tmp_path), "first_message": "Go."})
        entry = hub.entry(SESSION)

        def completed() -> int:
            return sum(
                1
                for f in frames
                if f.get("type") == "session.event" and f["event"]["kind"] == "turn_completed"
            )

        await wait_for(lambda: completed() == 1 and entry.session.turn is None)
        later = time.monotonic() + idle.IDLE_RELEASE + 1
        assert await hub.idle.sweep(later) == 0, "a background command is still to come"
        await wait_for(lambda: completed() == 2 and entry.session.turn is None)
        assert await hub.idle.sweep(later) == 1
        assert entry.runner is None
        assert (entry.session.control, entry.session.state) == ("remote", "idle")
    finally:
        await hub.close()
        db.close()


async def test_a_holder_takes_the_conversation_from_a_process_with_no_turn_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 1 of 4.4 is the device's own turn alone: agy's background command does not keep it."""
    monkeypatch.setattr(runner_module, "WAKE_QUIET", 0.2)
    binary, _ = fake_agy(tmp_path, turns=["wake"], wake_delay=30)
    db = Registry(tmp_path / "state.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(db, publish, "dev-1", lambda: [agent_info(binary)])

    async def holder(binary: str | None, *, cwds: bool) -> Holders:
        return Holders([Holder(4242, SESSION)])

    monkeypatch.setattr(presence, "scan", holder)
    try:
        await hub.create({"agent": "antigravity", "cwd": str(tmp_path), "first_message": "Go."})
        entry = hub.entry(SESSION)
        await wait_for(lambda: entry.session.turn is None and entry.session.state == "idle")
        runner = entry.runner
        assert isinstance(runner, runner_module.AntigravityRunner)
        assert runner.private_process is False, "agy's background command runs on"
        holding = Holding(hub, lambda e: Place(e.session.cwd))
        await holding.scan(None)
        assert entry.runner is None
        assert (entry.session.control, entry.session.state) == ("terminal", "readonly")
    finally:
        await hub.close()
        db.close()
