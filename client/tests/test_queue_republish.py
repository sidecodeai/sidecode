"""A gateway link that comes up is told again what each session has held in its Up next.

The gateway keeps only the newest `queue` snapshot of a session, in its memory, and history carries
no `queue` event, so a gateway that restarted while the device went on has lost every snapshot and
shows no Up next until the queue next changes. The device says it again after every connect, with
the event a change publishes and the summary that follows it, neither of which counts as activity.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client import config as config_module
from rc_client.agents.claude import transcripts
from rc_client.agents.claude.holders import HolderScan
from rc_client.agents.codex import rollouts
from rc_client.registry import Registry
from rc_client.sessions import hub as hub_module
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.hub import SessionEntry
from tests.helpers import tick
from tests.test_first_hello import restarted_daemon
from tests.test_session_start import Harness
from tests.test_stale_queue import leave_behind


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)
    built = Harness(tmp_path)
    yield built
    await built.hub.close()
    built.close()


async def holding(harness: Harness, session_id: str, pid: int, *texts: str) -> SessionEntry:
    """An attached session whose CLI is mid-turn, with these messages held for the turn's end."""
    await harness.attach(session_id, pid=pid)
    entry = harness.hub.entry(session_id)
    await tick(harness.hub, entry, running=True)
    for number, text in enumerate(texts):
        sent = await harness.hub.send(
            {
                "id": f"{session_id}-{number}",
                "session_id": session_id,
                "text": text,
                "mode": "queue",
            }
        )
        assert sent["accepted"] == "queued"
    return entry


def queues(harness: Harness, since: int = 0) -> dict[str, list[dict[str, Any]]]:
    return {
        frame["session_id"]: frame["event"]["pending"]
        for frame in harness.frames[since:]
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
    }


def summaries(harness: Harness, since: int = 0) -> dict[str, dict[str, Any]]:
    return {
        frame["session"]["session_id"]: frame["session"]
        for frame in harness.frames[since:]
        if frame.get("type") == "session.updated"
    }


async def test_every_session_with_messages_held_is_told_what_they_are(harness: Harness) -> None:
    await holding(harness, "held-1", 101, "first", "second")
    await holding(harness, "held-2", 102, "only")
    await harness.attach("idle-1", pid=103)
    before = len(harness.frames)

    await harness.hub.republish_queues()

    said = queues(harness, before)
    assert sorted(said) == ["held-1", "held-2"], "a session with nothing held is not mentioned"
    assert [(item["id"], item["text"]) for item in said["held-1"]] == [
        ("held-1-0", "first"),
        ("held-1-1", "second"),
    ]
    assert [(item["id"], item["text"]) for item in said["held-2"]] == [("held-2-0", "only")]


async def test_what_is_said_is_the_queue_as_it_stands_now(harness: Harness) -> None:
    entry = await holding(harness, "held-1", 101, "first", "second")
    await harness.hub.queue_remove({"session_id": "held-1", "queued_id": "held-1-0"})
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert [item["id"] for item in queues(harness, before)["held-1"]] == ["held-1-1"]
    assert entry.session.queued == 1


async def test_saying_it_again_is_not_activity_and_moves_no_session(harness: Harness) -> None:
    held = await holding(harness, "held-1", 101, "first")
    quiet = await holding(harness, "held-2", 102, "only")
    held.session.updated_at, quiet.session.updated_at = 1_700_000_000_000, 1_700_000_100_000
    before = len(harness.frames)

    await harness.hub.republish_queues()

    after = summaries(harness, before)
    assert {sid: item["updated_at"] for sid, item in after.items()} == {
        "held-1": 1_700_000_000_000,
        "held-2": 1_700_000_100_000,
    }
    assert {sid: item["queued"] for sid, item in after.items()} == {"held-1": 1, "held-2": 1}
    assert (held.session.updated_at, quiet.session.updated_at) == (
        1_700_000_000_000,
        1_700_000_100_000,
    )


async def test_a_session_nothing_holds_is_told_too(harness: Harness) -> None:
    """Its queue waits for the next send, and apps show it as Up next all the same."""
    dormant = harness.seed("dormant-1")
    dormant.queue.append({"id": "q-1", "text": "later", "ts": 5, "attachments": []})
    dormant.session.queued = 1
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert [(item["id"], item["text"]) for item in queues(harness, before)["dormant-1"]] == [
        ("q-1", "later")
    ]


async def test_nothing_is_said_when_nothing_is_held(harness: Harness) -> None:
    await harness.attach("idle-1", pid=101)
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert harness.frames[before:] == []


async def test_it_is_said_again_each_time(harness: Harness) -> None:
    await holding(harness, "held-1", 101, "first")
    before = len(harness.frames)

    await harness.hub.republish_queues()
    await harness.hub.republish_queues()

    said = [
        frame
        for frame in harness.frames[before:]
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
    ]
    assert len(said) == 2
    assert said[0]["event"]["seq"] < said[1]["event"]["seq"]


async def test_a_session_removed_while_it_is_being_told_is_skipped(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = await holding(harness, "held-1", 101, "first")
    await holding(harness, "held-2", 102, "only")
    emit = held.channel.emit

    async def removing_the_next(kind: str, **fields: Any) -> dict[str, Any]:
        harness.hub.entries.pop("held-2", None)
        return await emit(kind, **fields)

    monkeypatch.setattr(held.channel, "emit", removing_the_next)
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert sorted(queues(harness, before)) == ["held-1"]


# --------------------------------------------- a queue that changed with the link down


class Link:
    """A gateway link a test brings down and up: frames published while it is down are dropped."""

    def __init__(self, harness: Harness) -> None:
        self.up = True
        harness.hub.link_up = lambda: self.up

        async def lossy(frame: dict[str, Any]) -> None:
            if self.up:
                harness.frames.append(frame)

        # Before any session exists, so that every channel is made with it.
        harness.hub.publish = lossy


async def test_a_queue_that_emptied_while_the_link_was_down_is_said_empty_on_the_next_link(
    harness: Harness,
) -> None:
    """The frame that emptied it was dropped, and the gateway would serve the old snapshot on."""
    link = Link(harness)
    await holding(harness, "held-1", 101, "later")
    assert [item["id"] for item in queues(harness)["held-1"]] == ["held-1-0"]

    link.up = False
    await harness.hub.queue_remove({"session_id": "held-1", "queued_id": "held-1-0"})
    assert harness.hub.entry("held-1").queue == []
    link.up = True
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert queues(harness, before) == {"held-1": []}


async def test_a_queue_that_emptied_while_the_link_was_up_is_not_said_again(
    harness: Harness,
) -> None:
    Link(harness)
    await holding(harness, "held-1", 101, "later")
    await harness.hub.queue_remove({"session_id": "held-1", "queued_id": "held-1-0"})
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert queues(harness, before) == {}


async def test_a_queue_that_changed_in_the_dark_is_said_once(harness: Harness) -> None:
    link = Link(harness)
    await holding(harness, "held-1", 101, "later")
    link.up = False
    await harness.hub.queue_remove({"session_id": "held-1", "queued_id": "held-1-0"})
    link.up = True

    await harness.hub.republish_queues()
    before = len(harness.frames)
    await harness.hub.republish_queues()

    assert queues(harness, before) == {}


async def test_a_queue_that_changed_in_the_dark_but_is_not_empty_is_said_as_it_stands(
    harness: Harness,
) -> None:
    link = Link(harness)
    await holding(harness, "held-1", 101, "first")
    link.up = False
    await harness.hub.send(
        {"id": "held-1-late", "session_id": "held-1", "text": "second", "mode": "queue"}
    )
    link.up = True
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert [item["id"] for item in queues(harness, before)["held-1"]] == ["held-1-0", "held-1-late"]


async def test_a_session_removed_while_the_link_was_down_is_not_spoken_for(
    harness: Harness,
) -> None:
    link = Link(harness)
    await holding(harness, "held-1", 101, "later")
    link.up = False
    await harness.hub.queue_remove({"session_id": "held-1", "queued_id": "held-1-0"})
    harness.hub.entries.pop("held-1")
    link.up = True
    before = len(harness.frames)

    await harness.hub.republish_queues()

    assert queues(harness, before) == {}
    assert harness.hub._unsaid_queues == set()


# ------------------------------------------------------------ the whole daemon


async def test_the_daemon_says_it_after_every_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = Registry(config_module.database_path())
    leave_behind(previous, {"held-1": 0})
    previous.close()

    async def no_terminals() -> HolderScan:
        return HolderScan(holders=[], complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", no_terminals)
    monkeypatch.setattr(rollouts, "SESSIONS_DIR", tmp_path / "agent-state" / "codex")
    async with restarted_daemon() as (daemon, server):
        await server.wait_for("hello", timeout=10)
        assert [f for f in server.frames("session.event") if f["event"]["kind"] == "queue"] == []

        # A message is held after the first connect, and the gateway then loses its memory of it.
        held = daemon.hub.entry("held-1")
        held.queue.append({"id": "m-1", "text": "next", "ts": 7, "attachments": []})
        held.session.queued = 1
        for round_number in (1, 2):
            server.received.clear()
            await server.close_current()

            async def said() -> dict[str, Any]:
                while True:
                    for frame in server.frames("session.event"):
                        if frame["event"]["kind"] == "queue":
                            return frame
                    await asyncio.sleep(0.01)

            frame = await asyncio.wait_for(said(), timeout=15)
            assert frame["session_id"] == "held-1", round_number
            assert [(item["id"], item["text"]) for item in frame["event"]["pending"]] == [
                ("m-1", "next")
            ]
            # After the hello of the new link, never before it.
            assert server.received.index(frame) > server.received.index(server.frames("hello")[0])
