"""Amendment A27: slash commands are listed and run through the hub."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Choice, Command, Session
from rc_client.sessions.hub import SessionHub

from .test_hub import FakeRunner, add_session, build_hub

REQUEST = "5c1d7e2a-9b3f-4a8c-8d6e-0f1a2b3c4d5e"


def commanding_agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["takeover", "interrupt", "queue", "history", "commands"],
        ),
    ]


def commanding_hub(tmp_path: Path) -> tuple[SessionHub, list[dict[str, Any]]]:
    hub, frames, _registry = build_hub(tmp_path)
    hub._agents = commanding_agents
    return hub, frames


def test_command_serialises_without_its_optional_fields() -> None:
    assert Command("compact", "Summarise").to_dict() == {
        "name": "compact",
        "description": "Summarise",
    }
    assert Command("review", "Review", argument="instructions", group="Built-in").to_dict() == {
        "name": "review",
        "description": "Review",
        "argument": "instructions",
        "group": "Built-in",
    }


async def test_commands_come_from_the_live_runner(tmp_path: Path) -> None:
    hub, _frames = commanding_hub(tmp_path)
    add_session(hub)
    result = await hub.commands({"session_id": "sess-1"})
    assert result == {
        "commands": [
            {"name": "compact", "description": "Summarise the conversation", "group": "Built-in"}
        ]
    }


async def test_commands_are_unsupported_without_the_capability(tmp_path: Path) -> None:
    hub, _frames, _registry = build_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as caught:
        await hub.commands({"session_id": "sess-1"})
    assert caught.value.code == "unsupported"
    with pytest.raises(RcError) as caught:
        await hub.command({"session_id": "sess-1", "name": "compact", "id": REQUEST})
    assert caught.value.code == "unsupported"


async def test_a_session_without_a_process_answers_from_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _frames = commanding_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = None

    async def offline(session: Session) -> list[Command]:
        assert session.session_id == "sess-1"
        return [Command("review", "Review the changes", argument="instructions")]

    monkeypatch.setattr(registry.plugin("claude"), "commands", offline, raising=False)
    result = await hub.commands({"session_id": "sess-1"})
    assert result["commands"] == [
        {"name": "review", "description": "Review the changes", "argument": "instructions"}
    ]


async def test_a_session_without_a_process_or_a_plugin_list_offers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _frames = commanding_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = None
    monkeypatch.delattr(registry.plugin("claude"), "commands", raising=False)
    assert await hub.commands({"session_id": "sess-1"}) == {"commands": []}


async def test_command_runs_under_the_request_id_and_is_idempotent(tmp_path: Path) -> None:
    hub, frames = commanding_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    params = {"session_id": "sess-1", "name": "compact", "argument": " now ", "id": REQUEST}
    assert await hub.command(params) == {}
    assert runner.commands_run == [("compact", "now", REQUEST)]
    echoes = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]
    assert echoes[-1]["block_id"] == REQUEST
    assert echoes[-1]["text"] == "/compact now"
    assert await hub.command(params) == {}
    assert len(runner.commands_run) == 1


async def test_command_strips_a_leading_slash_and_requires_a_name(tmp_path: Path) -> None:
    hub, _frames = commanding_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.command({"session_id": "sess-1", "name": "/compact", "id": REQUEST})
    assert runner.commands_run[0][0] == "compact"
    with pytest.raises(RcError) as caught:
        await hub.command({"session_id": "sess-1", "name": "  "})
    assert caught.value.code == "bad_request"


async def test_an_unknown_command_is_not_found(tmp_path: Path) -> None:
    hub, _frames = commanding_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as caught:
        await hub.command({"session_id": "sess-1", "name": "dance", "id": REQUEST})
    assert caught.value.code == "not_found"


async def test_a_command_waits_for_no_turn_and_refuses_a_running_one(tmp_path: Path) -> None:
    hub, _frames = commanding_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"session_id": "sess-1", "text": "go", "mode": "auto"})
    with pytest.raises(RcError) as caught:
        await hub.command({"session_id": "sess-1", "name": "compact", "id": REQUEST})
    assert caught.value.code == "conflict"
    assert runner.commands_run == []


async def test_a_command_on_a_terminal_session_is_a_conflict(tmp_path: Path) -> None:
    hub, _frames = commanding_hub(tmp_path)
    add_session(hub, control="terminal")
    with pytest.raises(RcError) as caught:
        await hub.command({"session_id": "sess-1", "name": "compact", "id": REQUEST})
    assert caught.value.code == "conflict"
