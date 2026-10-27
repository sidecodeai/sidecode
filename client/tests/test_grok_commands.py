"""Amendment A27: the slash commands a Grok session advertises, and running one.

`fixtures/grok/available-commands.json` is the advertisement of a Grok Build
1.0.30 session, recorded over ACP, with the recording machine's home directory
rewritten and its personal skills and plugin replaced by neutral examples.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.grok import commands as grok_commands
from rc_client.agents.grok import plugin as grok_plugin
from rc_client.agents.grok.commands import (
    EXCLUDED,
    MAX_ARGUMENT,
    MAX_DESCRIPTION,
    NAME_PATTERN,
    advertised,
    recall,
    remember,
    store_path,
)
from rc_client.agents.registry import offline_commands
from rc_client.errors import RcError
from rc_client.models import Command, Session

from .test_grok_runner import build, wait_for

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "grok" / "available-commands.json"
# The channel rewrites any block id that is not already a UUID, so a test that
# checks the app's own id back has to send one, exactly as an app does (A12).
BLOCK = "7f3a1c02-4d58-4e6b-9a21-5c8e0b7d4f19"
SECOND_BLOCK = "2d9b6e14-8c37-4a52-b0fd-6e1a93c7d825"
# What the recorded advertisement carries and the device leaves out. `rename` and
# `title` are excluded ahead of a Grok Build that advertises them (A55).
RECORDED_EXCLUSIONS = {"always-approve", "context", "statusline"}


def advertisement() -> dict[str, Any]:
    notification: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    update: dict[str, Any] = notification["params"]["update"]
    return update


def by_name(listed: list[Command]) -> dict[str, Command]:
    return {command.name: command for command in listed}


# ------------------------------------------------------------------- the mapping


def test_the_real_advertisement_maps_onto_the_protocol_s_commands() -> None:
    update = advertisement()
    assert len(update["availableCommands"]) == 40
    listed = advertised(update)
    assert len(listed) == 40 - len(RECORDED_EXCLUSIONS)
    found = by_name(listed)

    assert found["compact"] == Command(
        name="compact",
        description="Compress conversation history to save context window",
        argument="optional context about what to preserve",
        group="Built-in",
    )
    assert found["hooks-list"].argument is None
    # Grok says where each entry comes from in the `_meta` of the advertisement.
    assert found["session-info"].group == "Built-in"
    assert found["review-helper"].group == "Skills"
    assert found["example:review"].group == "Plugins"
    assert found["deep-research"].group == "Workflows"

    for command in listed:
        assert NAME_PATTERN.match(command.name)
        assert command.description and "\n" not in command.description
        assert len(command.description) <= MAX_DESCRIPTION
        assert command.argument is None or len(command.argument) <= MAX_ARGUMENT


def test_settings_and_terminal_only_commands_are_never_listed() -> None:
    raw = {entry["name"] for entry in advertisement()["availableCommands"]}
    offered = {command.name for command in advertised(advertisement())}
    for name in RECORDED_EXCLUSIONS:
        assert name in raw, f"{name} is no longer advertised; the exclusion is dead"
        assert name not in offered
    assert set(EXCLUDED) >= RECORDED_EXCLUSIONS
    assert all(EXCLUDED.values())


def test_naming_a_session_is_a_setting_and_never_a_command() -> None:
    """A55: `session.set` carries the name, so `/rename` and its `/title` alias stay unlisted."""
    update = {
        "availableCommands": [
            {"name": "rename", "description": "Rename the current session"},
            {"name": "title", "description": "Rename the current session"},
            {"name": "session-info", "description": "Show session details"},
        ]
    }
    assert [command.name for command in advertised(update)] == ["session-info"]
    remember(
        [
            Command(name="rename", description="Rename the current session"),
            Command(name="session-info", description="Show session details"),
        ]
    )
    assert [command.name for command in recall()] == ["session-info"]


def test_a_paragraph_of_a_description_becomes_one_line() -> None:
    found = by_name(advertised(advertisement()))
    skill = found["example-skill"]
    assert skill.description.endswith("…")
    assert skill.description.startswith("An example personal skill")
    assert len(skill.description) <= MAX_DESCRIPTION
    # A usage line long enough to crowd out the composer is cut the same way.
    assert found["run"].argument is not None
    assert found["run"].argument.endswith("…")


def test_entries_this_device_cannot_offer_are_dropped() -> None:
    listed = advertised(
        {
            "availableCommands": [
                "not an object",
                {"name": ""},
                {"name": "Shouting", "description": "a name no app could filter"},
                {"name": "ok", "description": "kept"},
                {"name": "ok", "description": "a second entry for the same name"},
            ]
        }
    )
    assert listed == [Command(name="ok", description="kept", group="Built-in")]
    # A name with no description still reads as itself rather than as a blank row.
    assert advertised({"availableCommands": [{"name": "bare"}]})[0].description == "bare"
    assert advertised({}) == []


# --------------------------------------------------------------------- the store


def test_the_list_survives_a_restart() -> None:
    listed = advertised(advertisement())
    assert recall() == []
    remember(listed)
    assert recall() == listed
    assert store_path().name == "grok-commands.json"


def test_an_unchanged_list_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = advertised(advertisement())
    remember(listed)
    written: list[Path] = []
    monkeypatch.setattr(
        grok_commands,
        "write_atomic",
        lambda path, data, mode=0o600: written.append(path),
    )
    remember(listed)
    assert written == []
    remember(listed[:3])
    assert written == [store_path()]


def test_a_store_this_build_cannot_read_offers_nothing() -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert recall() == []
    path.write_text(json.dumps({"version": 99, "commands": [{"name": "x", "description": "y"}]}))
    assert recall() == []


def test_a_stale_entry_this_build_excludes_is_dropped_on_the_way_out() -> None:
    remember(
        [
            Command(name="context", description="Show context window usage"),
            Command(name="session-info", description="Show session details"),
        ]
    )
    assert recall() == [Command(name="session-info", description="Show session details")]


# -------------------------------------------------------------------- the runner


async def test_the_runner_offers_what_the_session_advertised(tmp_path: Path) -> None:
    runner, _recorder, _peer = build(tmp_path, "command")
    await runner.start()
    await wait_for(lambda: bool(runner._commands))
    listed = await runner.commands()
    await runner.close()

    names = {command.name for command in listed}
    assert {"compact", "hooks-list", "session-info"} <= names
    assert "context" not in names
    # The device keeps it, so a session with no process can still answer.
    assert recall() == listed


async def test_a_command_runs_as_a_turn_under_the_app_s_own_block_id(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "command")
    await runner.start()
    await wait_for(lambda: bool(runner._commands))
    await runner.command("hooks-list", None, BLOCK)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    echo = recorder.events("user_message")[-1]
    assert echo["block_id"] == BLOCK
    assert echo["text"] == "/hooks-list"
    assert echo["source"] == "remote"
    assert peer.params("session/prompt")["prompt"] == [{"type": "text", "text": "/hooks-list"}]
    # Grok answers a shell-side command as an ordinary message, at no token cost.
    assert recorder.events("assistant_text")[-1]["text"].startswith("Loaded hooks (2):")
    completed = recorder.events("turn_completed")[0]
    assert completed["stop_reason"] == "completed"
    assert "usage" not in completed


async def test_an_argument_follows_the_name(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "command")
    await runner.start()
    await wait_for(lambda: bool(runner._commands))
    await runner.command("compact", "keep the plan", SECOND_BLOCK)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("user_message")[-1]["text"] == "/compact keep the plan"
    assert peer.params("session/prompt")["prompt"] == [
        {"type": "text", "text": "/compact keep the plan"}
    ]


async def test_a_name_the_session_never_advertised_is_not_found(tmp_path: Path) -> None:
    runner, _recorder, peer = build(tmp_path, "command")
    await runner.start()
    await wait_for(lambda: bool(runner._commands))
    with pytest.raises(RcError) as caught:
        await runner.command("context", None, BLOCK)
    await runner.close()
    assert caught.value.code == "not_found"
    assert peer.calls("session/prompt") == []


async def test_a_session_that_has_not_advertised_yet_falls_back_to_the_device_s_list(
    tmp_path: Path,
) -> None:
    """The advertisement is a notification, so it can lag the session by a beat."""
    listed = [Command(name="hooks-list", description="Show hooks", group="Built-in")]
    remember(listed)
    runner, _recorder, _peer = build(tmp_path, "command")
    assert await runner.commands() == listed


# -------------------------------------------------------------------- the plugin


async def test_the_plugin_answers_for_a_session_with_no_process() -> None:
    assert "commands" in grok_plugin.CAPABILITIES
    listed = advertised(advertisement())
    remember(listed)
    session = Session(session_id="sess-1", device_id="d1", agent="grok", cwd="/tmp")
    assert await grok_plugin.commands(session) == listed
    assert await offline_commands("grok", session) == listed
