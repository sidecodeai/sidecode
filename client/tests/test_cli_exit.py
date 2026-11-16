"""A Claude CLI that quits leaves the list as it quits, not at the next scan.

The bridge of a CLI closes before the CLI itself has finished: it flushes its
transcript and runs its own hooks first. The device watches the CLI's own pid for
its end, as the kernel reports it, instead of looking once a second later and
leaving the session to the terminal until the next scan.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client import exits
from rc_client.models import AgentInfo
from rc_client.registry import Registry
from rc_client.sessions import hub as hub_module
from rc_client.sessions.attach import Attachment
from rc_client.sessions.hub import SessionEntry, SessionHub


async def sleeper(seconds: float) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec("sleep", str(seconds))


async def stop(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    await process.wait()


# ---------------------------------------------------------------- the watch


async def test_the_end_of_a_process_is_heard_as_it_happens() -> None:
    process = await sleeper(0.2)
    started = time.monotonic()
    assert await exits.ended(process.pid, 5.0) is True
    assert time.monotonic() - started < 2.0
    await process.wait()


async def test_a_process_that_runs_on_is_not_called_ended() -> None:
    process = await sleeper(30)
    try:
        assert await exits.ended(process.pid, 0.2) is False
    finally:
        await stop(process)


async def test_a_pid_nobody_has_is_ended_at_once() -> None:
    process = await sleeper(0)
    await process.wait()
    assert await exits.ended(process.pid, 5.0) is True


async def test_without_the_kernel_s_word_the_pid_is_looked_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exits, "_watch", lambda pid: None)
    process = await sleeper(0.2)
    assert await exits.ended(process.pid, 5.0) is True
    await process.wait()
    running = await sleeper(30)
    try:
        assert await exits.ended(running.pid, 0.2) is False
    finally:
        await stop(running)


# ------------------------------------------------------------------ the hub


class Bridge(Attachment):
    def __init__(self, pid: int) -> None:
        super().__init__(session_id="s-1", cwd="/repo", pid=pid, claude_version="2.1.300")

    async def inject(self, message_id: str, text: str) -> bool:
        return True

    def detach(self) -> None:
        return None


def agents() -> list[AgentInfo]:
    return [AgentInfo(agent="claude", available=True, attach="channel", attach_ready=True)]


@pytest.fixture
async def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SessionHub]:
    from rc_client.agents.claude import transcripts

    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.3)
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    built = SessionHub(registry, publish, "dev-1", agents)
    yield built
    await built.close()
    registry.close()


async def attached(hub: SessionHub, pid: int) -> tuple[SessionEntry, Bridge]:
    bridge = Bridge(pid)
    await hub.attach_registered(bridge)
    entry = hub.entry("s-1")
    # Something was said, so the session is no ghost to be withdrawn when it is let go.
    await entry.channel.emit("user_message", block_id="u1", text="hi", source="terminal")
    return entry, bridge


async def test_a_cli_that_quits_with_its_bridge_is_let_go_without_a_scan(
    hub: SessionHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = await sleeper(30)
    entry, bridge = await attached(hub, process.pid)
    scans: list[str] = []

    async def holder(entry: SessionEntry) -> str:
        scans.append(entry.session.session_id)
        return "terminal"

    monkeypatch.setattr(hub, "_holder_control", holder)
    await stop(process)
    await hub.attach_closed(bridge)
    assert entry.session.control == "none"
    assert scans == []


async def test_a_cli_that_quits_after_the_settle_is_let_go_as_it_quits(
    hub: SessionHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = await sleeper(30)
    entry, bridge = await attached(hub, process.pid)

    async def holder(entry: SessionEntry) -> str:
        entry.holder_pid = process.pid
        return "terminal"

    monkeypatch.setattr(hub, "_holder_control", holder)
    await hub.attach_closed(bridge)
    # Still running after the settle: the terminal has it, and its end is watched for.
    assert entry.session.control == "terminal"
    assert entry.session.state == "readonly"
    await stop(process)

    async def let_go() -> None:
        while entry.session.control != "none":
            await asyncio.sleep(0.02)

    await asyncio.wait_for(let_go(), timeout=2)
    assert hub.entry("s-1").session.to_dict()["state"] == "idle"


async def test_a_cli_that_lives_on_keeps_the_session(
    hub: SessionHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "EXIT_WATCH", 0.3)
    process = await sleeper(30)
    try:
        entry, bridge = await attached(hub, process.pid)

        async def holder(entry: SessionEntry) -> str:
            entry.holder_pid = process.pid
            return "terminal"

        monkeypatch.setattr(hub, "_holder_control", holder)
        await hub.attach_closed(bridge)
        await asyncio.sleep(0.6)
        assert entry.session.control == "terminal"
    finally:
        await stop(process)
