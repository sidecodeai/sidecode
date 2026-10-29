"""Hooks and bridges that arrive close together are applied in the order they came.

Each of them is held up by a look at the process, or at Claude Code's record, and the order in
which those finish is not the order in which the things arrived. A bridge registering while its
CLI's hook is heard used to be placed by the hook's old word and stay there until the next hook, and
two hooks of one CLI could be applied the wrong way round, leaving the attachment on the session it
had left.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rc_client.agents.claude import live, transcripts
from rc_client.sessions import hub as hub_module
from rc_client.sessions.attach import SessionStart
from rc_client.sessions.bindings import TerminalBindings
from tests.test_session_start import TERMINAL_PID, Harness

STARTED = "Tue Oct 27 14:39:20 2026"


class Processes:
    """The start time of the CLI, each look at it taking as long as the test says."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, pid: int) -> str | None:
        await asyncio.sleep(self.delays.pop(0) if self.delays else 0.0)
        return STARTED


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)
    built = Harness(tmp_path)
    built.hub.process_start = Processes()
    yield built
    await built.hub.close()
    built.close()


def remember(harness: Harness, session_id: str) -> None:
    harness.hub.bindings.remember(
        SessionStart(session_id, "/repo", TERMINAL_PID, "resume", ""), STARTED
    )


# ------------------------------------------- a hook heard while a bridge registers


async def test_a_hook_heard_while_the_record_is_being_read_is_not_missed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    remember(harness, "s-old")
    real = live.read_records

    def slow() -> dict[int, live.LiveRecord]:
        time.sleep(0.3)
        return real()

    monkeypatch.setattr(live, "read_records", slow)

    registering = asyncio.create_task(harness.attach("s-env"))
    await asyncio.sleep(0.1)
    await harness.hook("s-new", source="clear")
    attachment = await asyncio.wait_for(registering, timeout=5)

    assert attachment.session_id == "s-new"
    assert harness.hub.entry("s-new").session.control == "shared"


async def test_a_hook_heard_between_placing_a_bridge_and_attaching_it_moves_it(
    harness: Harness,
) -> None:
    """The hook cannot move a bridge that is not attached, so the registration follows it."""
    remember(harness, "s-old")
    old = harness.seed("s-old")
    async with old.lock:
        registering = asyncio.create_task(harness.attach("s-env"))
        await asyncio.sleep(0.1)
        assert not registering.done(), "the bridge is placed on s-old and waits for its lock"
        await harness.hook("s-new", source="clear")
    attachment = await asyncio.wait_for(registering, timeout=5)

    assert attachment.session_id == "s-new"
    assert harness.hub.entry("s-new").session.control == "shared"
    assert harness.hub.entry("s-new").shared is not None
    assert "s-old" not in harness.hub.entries or harness.hub.entry("s-old").shared is None


async def test_a_hook_heard_before_the_bridge_registered_is_not_acted_on_twice(
    harness: Harness,
) -> None:
    remember(harness, "s-old")
    attachment = await harness.attach("s-env")

    assert attachment.session_id == "s-old"
    assert list(harness.hub.entries) == ["s-old"]


# ------------------------------------------------- two hooks of one CLI, close together


async def test_two_hooks_of_one_cli_are_applied_in_the_order_they_arrived(
    harness: Harness,
) -> None:
    processes = harness.hub.process_start
    attachment = await harness.attach("s-env")
    # The first hook's look at the process takes longest, so it finishes after the second.
    assert isinstance(processes, Processes)
    processes.delays = [0.2, 0.0]

    first = asyncio.create_task(harness.hook("s-1", source="resume"))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(harness.hook("s-2", source="resume"))
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    binding = harness.hub.bindings.get(TERMINAL_PID)
    assert binding is not None and binding.start.session_id == "s-2"
    assert attachment.session_id == "s-2", "and the attachment did not go back to s-1"
    assert harness.hub.entry("s-2").shared is not None


# ------------------------------------------------------------------- the map itself


def test_a_hook_heard_earlier_than_the_one_held_says_nothing_new(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    bindings = TerminalBindings(harness.registry)
    later = SessionStart("later", "/repo", 7, "resume", "")
    earlier = SessionStart("earlier", "/repo", 7, "resume", "")

    assert bindings.remember(later, STARTED, at=200) is True
    assert bindings.remember(earlier, STARTED, at=100) is False

    held = bindings.get(7)
    assert held is not None and (held.start.session_id, held.at) == ("later", 200)
    harness.close()


def test_two_hooks_heard_at_the_same_moment_are_applied_as_they_are_processed(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    bindings = TerminalBindings(harness.registry)

    assert bindings.remember(SessionStart("a", "/repo", 7, "resume", ""), STARTED, at=100)
    assert bindings.remember(SessionStart("b", "/repo", 7, "resume", ""), STARTED, at=100)

    held = bindings.get(7)
    assert held is not None and held.start.session_id == "b"
    harness.close()


def test_a_hook_without_a_time_is_heard_now(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    bindings = TerminalBindings(harness.registry)
    before = bindings.stamp()

    assert bindings.remember(SessionStart("a", "/repo", 7, "resume", ""), STARTED)

    held = bindings.get(7)
    assert held is not None and held.at >= before
    harness.close()
