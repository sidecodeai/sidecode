"""A turn that leaves background tasks running is not over for the apps.

The `Stop` hook counts the subagents and workflows a turn leaves running. The
apps keep the turn open past it, so no "Turn finished" goes out while work the
person started is still under way; the CLI itself is idle, so nothing is held
back from it. A background shell is not counted, since some never end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.channel import turnhook
from rc_client.sessions import attach as attach_module
from rc_client.sessions.attach import TurnHook
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.limits import TurnEnd
from rc_client.sessions.shared import BACKGROUND_HOLD, PROMPT_GUARD
from tests.helpers import tick

from .test_shared_control import FakeAttachment
from .test_typist import Harness


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


def shown(entry: SessionEntry) -> str:
    """The state the apps are told, read afresh each time: it moves under the test's feet."""
    return entry.session.state


async def hook(harness: Harness, event: str, kind: str = "", background: int = 0) -> None:
    """One turn hook, as the attach server hands it to the hub."""
    await harness.hub.attach_turn(TurnHook("sess-1", event, kind, background=background))


async def turn_that_leaves(harness: Harness, background: int) -> SessionEntry:
    """A turn the person started, ended by a `Stop` that left `background` tasks running."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await hook(harness, "stop", background=background)
    await tick(harness.hub, entry, running=False)
    return entry


async def test_a_turn_that_leaves_a_workflow_running_stays_open_for_the_apps(
    harness: Harness,
) -> None:
    entry = await turn_that_leaves(harness, 1)
    state = entry.shared
    assert state is not None
    assert shown(entry) == "running"
    assert state.shown_busy
    assert harness.events("turn_completed") == []
    assert len(harness.events("turn_started")) == 1
    assert not state.busy, "the CLI itself is idle, which is what injecting waits for"


async def test_a_message_goes_into_the_idle_cli_whatever_the_tasks_it_left(
    harness: Harness,
) -> None:
    entry = await turn_that_leaves(harness, 1)
    sent = await harness.hub.send({"id": "r1", "session_id": "sess-1", "text": "and then?"})
    assert sent == {"accepted": "sent"}
    assert [text for _, text in harness.attachment.injected] == ["and then?"]
    assert entry.queue == []


async def test_a_claude_that_lists_no_tasks_ends_the_turn_at_its_stop(harness: Harness) -> None:
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await harness.hub.attach_turn(TurnHook("sess-1", "stop"))
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_completed")) == 1


async def test_a_turn_whose_only_background_task_is_a_shell_ends_at_its_stop(
    harness: Harness,
) -> None:
    """A dev server never ends, so its turn ends at the `Stop` as it did before the count."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    server = {
        "id": "b1",
        "type": "shell",
        "status": "running",
        "description": "Run the dev server",
        "command": "npm run dev",
    }
    stop = turnhook.frame(
        {"session_id": "sess-1", "hook_event_name": "Stop", "background_tasks": [server]}
    )
    assert stop is not None
    heard = attach_module._turn_hook(stop)
    assert heard is not None and heard.background == 0
    await harness.hub.attach_turn(heard)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_completed")) == 1
    assert entry.shared is not None and not entry.shared.shown_busy


@pytest.mark.parametrize("wakes", [0, 3])
async def test_the_turn_ends_once_when_the_last_task_is_done(harness: Harness, wakes: int) -> None:
    """Each end of a task wakes the CLI with a prompt of its own, and its `Stop` counts afresh."""
    entry = await turn_that_leaves(harness, 1)
    state = entry.shared
    assert state is not None
    for _ in range(wakes):
        await hook(harness, "prompt", "agent")
        assert state.background == 0
        await hook(harness, "stop", background=1)
        await tick(harness.hub, entry, running=False)
        assert shown(entry) == "running"
    await hook(harness, "prompt", "agent")
    assert shown(entry) == "running"
    await hook(harness, "stop", background=0)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_started")) == 1
    assert len(harness.events("turn_completed")) == 1


async def test_a_woken_turn_the_person_interrupts_leaves_nothing_working(harness: Harness) -> None:
    """An interrupted turn runs no `Stop`, so the prompt is what lets the hold go."""
    entry = await turn_that_leaves(harness, 1)
    await hook(harness, "prompt", "agent")
    state = entry.shared
    assert state is not None and state.prompted_at is not None
    state.prompted_at -= PROMPT_GUARD + 1
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert shown(entry) == "idle"
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"


async def test_a_slash_command_lets_nothing_go(harness: Harness) -> None:
    """It may only open a dialog, and no `Stop` follows one that does."""
    entry = await turn_that_leaves(harness, 2)
    await hook(harness, "prompt", "command")
    state = entry.shared
    assert state is not None and state.command_at is not None
    state.command_at -= PROMPT_GUARD + 1
    await tick(harness.hub, entry, running=False)
    assert state.background == 2
    assert shown(entry) == "running"


async def test_the_hold_lapses_after_a_subagents_thirty_minutes(harness: Harness) -> None:
    entry = await turn_that_leaves(harness, 1)
    state = entry.shared
    assert state is not None
    assert BACKGROUND_HOLD == 1800.0
    state.background_at -= BACKGROUND_HOLD - 5
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"
    state.background_at -= 10
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_completed")) == 1


async def test_stop_from_an_app_lets_the_hold_go(harness: Harness) -> None:
    """The CLI is idle and Escape stops none of the tasks: Stop means "I am done waiting"."""
    entry = await turn_that_leaves(harness, 1)
    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert harness.terminal.typed == []
    assert shown(entry) == "idle"
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"


async def test_a_cli_that_goes_away_takes_the_hold_with_it(harness: Harness) -> None:
    entry = await turn_that_leaves(harness, 1)
    await harness.hub.shared.closed(entry, harness.attachment, "none")
    assert entry.shared is None
    assert shown(entry) == "idle"
    assert harness.events("turn_completed")[-1]["stop_reason"] == "stopped"

    await harness.hub.attach_registered(FakeAttachment())
    attached = harness.hub.entry("sess-1")
    assert attached.shared is not None and attached.shared.background == 0
    assert shown(attached) == "idle"
