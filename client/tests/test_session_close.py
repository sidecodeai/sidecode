"""Amendment A39: closing a session the device drives, then the Archive."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_client.sessions import hub as hub_module
from tests.test_app_defects import StubSDK, claude_runner
from tests.test_hub import FakeRunner, add_session, build_hub
from tests.test_pi_runner import build as build_pi


def summaries(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every `session.updated` the hub published, in order."""
    return [frame["session"] for frame in frames if frame.get("type") == "session.updated"]


class RevivingRunner(FakeRunner):
    """A runner whose agent speaks its last while the close is under way.

    This is A15 arriving mid-close: a turn ends, a status goes idle, and the
    channel is asked to take the session out of the Archive.
    """

    async def shutdown(self) -> None:
        self.channel.session.archived = True
        await self.channel.revive()
        await super().shutdown()


class ClosingSDK(StubSDK):
    """The SDK client, counting the disconnect whose transport ends the CLI."""

    def __init__(self) -> None:
        super().__init__()
        self.disconnects = 0

    async def disconnect(self) -> None:
        self.disconnects += 1


class StuckRunner(FakeRunner):
    """An agent that will not die."""

    async def shutdown(self) -> None:
        await asyncio.sleep(30)


# --------------------------------------------------------------- the hub


async def test_closing_a_working_session_stops_it_and_says_so_once(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert runner.busy is True
    before = len(summaries(frames))

    result = await hub.archive({"session_id": "sess-1", "archived": True})

    assert runner.order == ["interrupt", "shutdown", "close"]
    assert entry.runner is None
    session = result["session"]
    assert (session["archived"], session["control"], session["state"]) == (True, "none", "stopped")
    assert session["turn"] is None
    assert session["state_detail"] is None
    # One publish for the whole close: the row goes from working to closed and
    # archived in a single step.
    assert summaries(frames)[before:] == [session]
    registry.close()


async def test_closing_an_idle_session_interrupts_nothing(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)

    await hub.archive({"session_id": "sess-1", "archived": True})

    assert runner.order == ["shutdown", "close"]
    assert entry.session.archived is True
    registry.close()


async def test_a_revive_racing_the_close_changes_nothing(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = RevivingRunner(entry.channel)
    before = len(summaries(frames))

    result = await hub.archive({"session_id": "sess-1", "archived": True})

    assert result["session"]["archived"] is True
    assert (entry.session.archived, entry.session.state) == (True, "stopped")
    assert summaries(frames)[before:] == [result["session"]]
    registry.close()


async def test_an_agent_that_will_not_die_is_let_go_of_anyway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "CLOSE_TIMEOUT", 0.05)
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = StuckRunner(entry.channel)

    result = await hub.archive({"session_id": "sess-1", "archived": True})

    assert hub.entry("sess-1").runner is None
    session = result["session"]
    assert (session["archived"], session["control"], session["state"]) == (True, "none", "stopped")
    registry.close()


async def test_unarchiving_only_clears_the_flag(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.archive({"session_id": "sess-1", "archived": True})

    result = await hub.archive({"session_id": "sess-1", "archived": False})

    assert result["session"]["archived"] is False
    # Nothing is started by clearing the flag: a message is what resumes it.
    assert hub.entry("sess-1").runner is None
    assert (entry.session.control, entry.session.state) == ("none", "stopped")
    registry.close()


async def test_a_session_closed_while_archived_stays_archived(tmp_path: Path) -> None:
    """The channel's guard, on its own: a close in flight owns the flag."""
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.session.archived = True
    entry.channel.closing = True
    before = len(summaries(frames))

    await entry.channel.revive()

    assert entry.session.archived is True
    assert summaries(frames)[before:] == []
    registry.close()


# ------------------------------------------------------- the agents' own end


async def test_closing_a_claude_session_ends_the_cli(tmp_path: Path) -> None:
    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    sdk = ClosingSDK()
    runner._client = sdk  # type: ignore[assignment]

    await runner.shutdown()

    assert sdk.disconnects == 1
    assert runner._client is None
    runner.channel.registry.close()


async def test_closing_a_pi_session_ends_the_process(tmp_path: Path) -> None:
    runner, _, _ = build_pi(tmp_path, "turn")
    await runner.start()
    process = runner._process
    assert process is not None
    running = process.alive

    await runner.shutdown()

    assert (running, process.alive) == (True, False)
    runner.channel.registry.close()
