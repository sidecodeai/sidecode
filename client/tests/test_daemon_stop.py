"""A daemon that stops does not tell the apps that the terminals it was attached to have.

Stopping the daemon closes every channel bridge, and a closing bridge used to be taken for its CLI
exiting: the turn the CLI was running ended as `stopped`, the session went back to the terminal as
`readonly`, the held messages were let go, and the shutdown waited a second for it. All of that was
wrong for a CLI that was still there, which is every one of them at an update, and the process that
started next had to derive it again anyway. Nothing of it is published now; the one thing that
does end is a prompt the device was relaying, which nothing can answer once it is gone.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts
from rc_client.channel import wire
from rc_client.channel.link import DaemonLink
from rc_client.config import Config
from rc_client.daemon import Daemon
from rc_client.sessions import hub as hub_module
from rc_client.sessions.attach import AttachServer
from rc_client.sessions.bindings import KEY
from rc_client.sessions.hub import SessionEntry
from tests.helpers import tick
from tests.test_session_start import TERMINAL_PID, FakeAttachment, Harness

SESSION = "attached-1"


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    # A shutdown that waited on every bridge would show here as a test that takes a minute.
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 30.0)
    built = Harness(tmp_path)
    yield built
    await built.hub.close()
    built.close()


async def working(harness: Harness) -> tuple[SessionEntry, FakeAttachment]:
    """A session whose CLI is in the middle of a turn, with a message held for its end."""
    attachment = await harness.attach(SESSION)
    entry = harness.hub.entry(SESSION)
    await tick(harness.hub, entry, running=True)
    held = await harness.hub.send(
        {"id": "m-1", "session_id": SESSION, "text": "next", "mode": "queue"}
    )
    assert held == {"accepted": "queued", "queued_id": "m-1"}
    return entry, attachment


def events(harness: Harness, since: int, kind: str) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in harness.frames[since:]
        if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
    ]


# ---------------------------------------------------- a bridge that closes

# What it was, and still is, when the CLI is the one that went.


async def test_a_bridge_that_closes_with_the_daemon_running_hands_the_session_back(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)
    entry, attachment = await working(harness)

    async def alive(entry: SessionEntry) -> str:
        return "terminal"

    monkeypatch.setattr(harness.hub, "_holder_control", alive)
    before = len(harness.frames)
    await harness.hub.attach_closed(attachment)

    assert entry.shared is None
    assert entry.session.control == "terminal"
    assert entry.session.state == "readonly"
    assert [event["stop_reason"] for event in events(harness, before, "turn_completed")] == [
        "stopped"
    ]


# ----------------------------------------------- the daemon that is stopping


async def test_a_bridge_that_closes_as_the_daemon_stops_changes_nothing(
    harness: Harness,
) -> None:
    entry, attachment = await working(harness)
    turn = entry.session.turn
    before = len(harness.frames)

    harness.hub.begin_close()
    await asyncio.wait_for(harness.hub.attach_closed(attachment), timeout=2)

    assert harness.frames[before:] == [], "nothing is published"
    assert entry.shared is not None and entry.shared.attachment is attachment
    assert entry.session.control == "shared"
    assert entry.session.state == "running"
    assert entry.session.turn is turn and turn is not None
    assert [item["id"] for item in entry.queue] == ["m-1"]
    assert entry.session.queued == 1


async def test_a_bridge_that_closed_just_before_the_daemon_began_to_stop_is_not_demoted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code reloading its MCP servers closes a bridge a moment before an update stops the
    daemon, and the settle that follows must not outlast the stop's own word on it."""
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.3)
    entry, attachment = await working(harness)
    turn = entry.session.turn
    before = len(harness.frames)

    closing = asyncio.create_task(harness.hub.attach_closed(attachment))
    await asyncio.sleep(0.05)
    assert not closing.done(), "the bridge is settling"
    harness.hub.begin_close()
    await asyncio.wait_for(closing, timeout=5)

    assert harness.frames[before:] == []
    assert entry.shared is not None and entry.shared.attachment is attachment
    assert entry.session.control == "shared"
    assert entry.session.turn is turn and turn is not None
    assert [item["id"] for item in entry.queue] == ["m-1"]


async def test_a_prompt_the_device_was_relaying_expires_with_the_daemon(
    harness: Harness,
) -> None:
    """Nothing can answer it once the daemon is gone, and the next process has no record of it."""
    entry, attachment = await working(harness)
    await harness.hub.attach_permission_request(
        attachment,
        {"request_id": "r-1", "tool_name": "Bash", "description": "run", "input_preview": "{}"},
    )
    assert str(entry.session.state) == "needs_approval"
    before = len(harness.frames)

    harness.hub.begin_close()
    await asyncio.wait_for(harness.hub.attach_closed(attachment), timeout=2)

    [approval] = events(harness, before, "approval")
    assert approval["status"] == "expired"
    assert entry.shared is not None and entry.shared.approvals == {}
    assert str(entry.session.state) == "running", "not asking for an answer nobody can give"
    assert events(harness, before, "turn_completed") == []
    assert entry.session.control == "shared"
    assert entry.session.turn is not None


async def test_what_a_hook_said_survives_the_stop(harness: Harness) -> None:
    """The restarted daemon puts the CLI back in the session its hook named."""
    harness.hub.process_start = started
    attachment = await harness.attach("started-in")
    await harness.hook(SESSION, source="resume")
    assert attachment.session_id == SESSION

    harness.hub.begin_close()
    await asyncio.wait_for(harness.hub.attach_closed(attachment), timeout=2)

    binding = harness.hub.bindings.get(TERMINAL_PID)
    assert binding is not None and binding.start.session_id == SESSION
    stored = json.loads(harness.registry.get_kv(KEY) or "[]")
    assert [item["session_id"] for item in stored] == [SESSION]


async def started(pid: int) -> str | None:
    return "Tue Oct 27 14:39:20 2026"


# -------------------------------------------- the daemon, and the real socket


async def test_the_daemon_stops_taking_bridges_for_exits_before_it_closes_their_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
        )
    )
    closing: list[bool] = []

    async def stop() -> None:
        closing.append(daemon.hub._closed)

    monkeypatch.setattr(daemon.attach, "stop", stop)
    await daemon.shutdown()
    assert closing == [True]


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory shallow enough for a Unix socket path (104 bytes on macOS)."""
    path = Path("/tmp") / f"rc-t-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def test_stopping_the_channel_socket_leaves_an_attached_session_alone(
    harness: Harness, short_dir: Path
) -> None:
    server = AttachServer(short_dir / "channel.sock", harness.hub)
    await server.start()

    async def ignore(message: dict[str, Any]) -> None:
        return None

    bridge = DaemonLink(short_dir / "channel.sock", ignore)
    bridge.register(wire.register("socket-1", "/repo", 4242, "2.1.267"))
    bridge.start()
    try:

        async def attached() -> None:
            while (
                "socket-1" not in harness.hub.entries
                or harness.hub.entry("socket-1").shared is None
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(attached(), timeout=5)
        entry = harness.hub.entry("socket-1")
        await tick(harness.hub, entry, running=True)
        before = len(harness.frames)

        harness.hub.begin_close()
        loop = asyncio.get_running_loop()
        stopping = loop.time()
        await asyncio.wait_for(server.stop(), timeout=5)

        assert loop.time() - stopping < 2, "the shutdown does not wait on its bridges"
        assert entry.shared is not None
        assert entry.session.control == "shared"
        assert entry.session.turn is not None
        assert events(harness, before, "turn_completed") == []
    finally:
        await bridge.stop()
