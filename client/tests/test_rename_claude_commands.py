"""A55: a rename is `session.set`, so Claude never offers `/rename` or `/name` as a command."""

from __future__ import annotations

from pathlib import Path

import pytest

from rc_client.agents.claude import plugin as claude_plugin
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.agents.claude.commands import COMMANDS
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.test_typist import Harness

RENAME_COMMANDS = {"rename", "name"}
REQUEST = "7d2e9c41-5b8a-4f36-9a1d-3c4e5f6a7b8c"


def test_the_commands_claude_lists_are_exactly_compact() -> None:
    assert [command.name for command in COMMANDS] == ["compact"]


async def test_a_session_with_no_process_lists_no_rename() -> None:
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/repo")
    listed = await claude_plugin.commands(session)
    assert RENAME_COMMANDS.isdisjoint(command.name for command in listed)


async def test_a_session_the_device_drives_lists_and_runs_no_rename(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, object]) -> None:
        return None

    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/repo")
    runner = ClaudeRunner(SessionChannel(registry, session, publish), binary=None, cwd="/repo")
    assert RENAME_COMMANDS.isdisjoint(command.name for command in await runner.commands())
    for name in RENAME_COMMANDS:
        with pytest.raises(RcError) as caught:
            await runner.command(name, "Ledger migration", REQUEST)
        assert caught.value.code == "not_found"
    registry.close()


async def test_an_attached_session_lists_no_rename_and_types_none(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    try:
        await harness.attach()
        listed = await harness.hub.commands({"session_id": "sess-1"})
        assert RENAME_COMMANDS.isdisjoint(command["name"] for command in listed["commands"])
        for name in RENAME_COMMANDS:
            with pytest.raises(RcError) as caught:
                await harness.hub.command({"session_id": "sess-1", "name": name, "id": REQUEST})
            assert caught.value.code == "not_found"
        assert harness.terminal.typed == []
    finally:
        harness.close()
