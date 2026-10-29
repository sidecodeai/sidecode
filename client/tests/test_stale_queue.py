"""A restarted device says that the Up next queues of the old process are empty.

The queue of a session is kept in the memory of the process that runs the daemon, so a restart
loses it, and `hub.load` restores every session with a count of zero. The gateway keeps the last
queue it was told for each session and serves it to every app that subscribes, and a gateway that
did not restart with the device still holds the old one: entries that answer `not_found` when an app
removes them. Nothing said otherwise until the session's next queue change.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client import config as config_module
from rc_client.agents.claude.holders import HolderScan
from rc_client.agents.codex import rollouts
from rc_client.config import Config
from rc_client.daemon import Daemon
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.hub import SessionHub
from tests.test_first_hello import restarted_daemon

UPDATED_AT = 1_700_000_000_000


class Published:
    """What a hub says, and whether anybody is listening."""

    def __init__(self, path: Path) -> None:
        self.frames: list[dict[str, Any]] = []
        self.registry = Registry(path)
        self.hub = SessionHub(self.registry, self.publish, "dev-1", lambda: [])

    async def publish(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def queues(self) -> dict[str, list[Any]]:
        return {
            frame["session_id"]: frame["event"]["pending"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
        }


def leave_behind(registry: Registry, queued: dict[str, int]) -> None:
    """The registry as a process that stopped with these many messages held for each session."""
    for session_id, held in queued.items():
        registry.upsert_session(
            Session(
                session_id=session_id,
                device_id="dev-1",
                agent="codex" if session_id.startswith("codex") else "claude",
                cwd="/repo",
                queued=held,
                updated_at=UPDATED_AT,
            )
        )
    registry.flush()


@pytest.fixture
async def restart(tmp_path: Path) -> AsyncIterator[Published]:
    first = Registry(tmp_path / "state.sqlite3")
    leave_behind(first, {"held-2": 2, "none-0": 0, "codex-1": 1})
    first.close()
    published = Published(tmp_path / "state.sqlite3")
    published.hub.load()
    yield published
    await published.hub.close()
    published.registry.close()


async def test_every_session_that_had_messages_held_is_announced_empty(
    restart: Published, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await restart.hub.clear_stale_queues()

    assert restart.queues() == {"held-2": [], "codex-1": []}
    told = [record for record in caplog.records if "queues of the last run" in record.getMessage()]
    assert [record.__dict__["context"] for record in told] == [{"sessions": 2}]
    assert all(entry.session.queued == 0 for entry in restart.hub.entries.values())


async def test_the_summary_follows_and_the_session_keeps_its_place_in_the_list(
    restart: Published,
) -> None:
    await restart.hub.clear_stale_queues()

    summaries = {
        frame["session"]["session_id"]: frame["session"]
        for frame in restart.frames
        if frame.get("type") == "session.updated"
    }
    assert sorted(summaries) == ["codex-1", "held-2"]
    assert [item["queued"] for item in summaries.values()] == [0, 0]
    assert {item["updated_at"] for item in summaries.values()} == {UPDATED_AT}


async def test_the_stored_count_is_corrected_too(restart: Published) -> None:
    await restart.hub.clear_stale_queues()

    stored = {session.session_id: session.queued for session in restart.registry.load_sessions()}
    assert stored == {"held-2": 0, "none-0": 0, "codex-1": 0}


async def test_nothing_is_said_while_the_link_is_down(restart: Published) -> None:
    restart.hub.link_up = lambda: False
    await restart.hub.clear_stale_queues()
    assert restart.frames == []

    restart.hub.link_up = lambda: True
    await restart.hub.clear_stale_queues()
    assert restart.queues() == {"held-2": [], "codex-1": []}


async def test_it_is_said_once(restart: Published) -> None:
    await restart.hub.clear_stale_queues()
    said = len(restart.frames)
    await restart.hub.clear_stale_queues()
    assert len(restart.frames) == said


async def test_a_session_that_was_removed_meanwhile_is_not_spoken_for(restart: Published) -> None:
    await restart.hub.delete({"session_id": "held-2"})

    await restart.hub.clear_stale_queues()

    assert restart.queues() == {"codex-1": []}


# ------------------------------------------------------------ the whole daemon


async def test_the_daemon_says_so_once_its_link_is_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = Registry(config_module.database_path())
    leave_behind(previous, {"held-2": 2, "none-0": 0})
    previous.close()

    async def no_terminals() -> HolderScan:
        return HolderScan(holders=[], complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", no_terminals)
    # The scan reads agent state from disk, and only what this test wrote may be in it.
    monkeypatch.setattr(rollouts, "SESSIONS_DIR", tmp_path / "agent-state" / "codex")
    async with restarted_daemon() as (_, server):
        hello = await server.wait_for("hello", timeout=10)
        assert {item["session_id"]: item["queued"] for item in hello["sessions"]} == {
            "held-2": 0,
            "none-0": 0,
        }

        async def said() -> dict[str, Any]:
            while True:
                for frame in server.frames("session.event"):
                    if frame["event"]["kind"] == "queue":
                        return frame
                await asyncio.sleep(0.01)

        frame = await asyncio.wait_for(said(), timeout=10)
        assert frame["session_id"] == "held-2"
        assert frame["event"]["pending"] == []
        # It came after the hello that announced the sessions, never before it.
        assert server.received.index(frame) > server.received.index(hello)
        queues = [
            item["session_id"]
            for item in server.frames("session.event")
            if item["event"]["kind"] == "queue"
        ]
        assert queues == ["held-2"]


async def test_a_failure_to_say_so_does_not_cost_the_first_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan is what the gateway backfills from; it runs whatever happened before it."""
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
        )
    )
    scanned: list[bool] = []
    republished: list[bool] = []

    async def failing() -> None:
        raise OSError("the registry is unwritable")

    async def republish() -> None:
        republished.append(True)

    async def scan(*, settings: bool = True) -> None:
        scanned.append(True)

    monkeypatch.setattr(daemon.hub, "clear_stale_queues", failing)
    monkeypatch.setattr(daemon.hub, "republish_queues", republish)
    monkeypatch.setattr(daemon.mirror, "scan_once", scan)

    await daemon._on_ready()

    assert scanned == [True]
    assert republished == [True], "one announcement failing does not cost the other"
    await daemon.hub.close()
    daemon.registry.close()
