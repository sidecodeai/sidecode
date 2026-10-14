"""Amendment A15: a session that comes back to life leaves the Archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rc_client.models import AgentInfo, Session
from rc_client.sessions.attach import Attachment
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.test_hub import FakeRunner, add_session, build_hub


def archived_session(session_id: str = "sess-1") -> Session:
    return Session(
        session_id=session_id,
        device_id="dev-1",
        agent="claude",
        cwd="/repo",
        state="idle",
        origin="terminal",
        control="none",
        archived=True,
    )


def load_archived(hub: SessionHub, session_id: str = "sess-1") -> SessionEntry:
    """Persist an archived session and read it back the way the daemon starts."""
    hub.registry.upsert_session(archived_session(session_id))
    hub.load()
    return hub.entry(session_id)


def with_fake_runner(hub: SessionHub, *, busy: bool = False) -> None:
    """Resume onto a fake agent instead of starting the real one."""

    async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
        runner = FakeRunner(entry.channel)
        runner._busy = busy
        return runner

    hub._build_runner = build  # type: ignore[method-assign]


def published(frames: list[dict[str, Any]], session_id: str = "sess-1") -> list[dict[str, Any]]:
    return [
        frame["session"]
        for frame in frames
        if frame.get("type") == "session.updated" and frame["session"]["session_id"] == session_id
    ]


async def test_an_archived_session_is_read_back_as_stopped(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = load_archived(hub)
    assert (entry.session.state, entry.session.control) == ("stopped", "none")
    assert entry.session.archived is True
    registry.close()


async def test_a_send_that_starts_a_turn_takes_the_session_out_of_the_archive(
    tmp_path: Path,
) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = load_archived(hub)
    with_fake_runner(hub)

    result = await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"})

    assert result == {"accepted": "sent"}
    assert entry.session.archived is False
    assert entry.session.state == "running"
    # Archiving had closed the runner and dropped control; resuming took it back.
    assert entry.session.control == "remote"
    assert published(frames)[-1]["archived"] is False
    registry.close()


async def test_a_send_that_only_joins_the_queue_takes_it_out_of_the_archive(
    tmp_path: Path,
) -> None:
    """No turn starts here, so the send itself has to clear `archived`."""
    hub, frames, registry = build_hub(tmp_path)
    entry = load_archived(hub)
    with_fake_runner(hub, busy=True)

    result = await hub.send({"id": "req-1", "session_id": "sess-1", "text": "later"})

    assert result == {"accepted": "queued", "queued_id": "req-1"}
    assert entry.session.archived is False
    assert entry.session.state != "stopped"
    assert published(frames)[-1]["archived"] is False
    registry.close()


async def test_archiving_a_running_session_leaves_no_control_behind(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.archive({"session_id": "sess-1", "archived": True})
    assert (entry.session.archived, entry.session.control) == (True, "none")

    with_fake_runner(hub)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "again"})
    assert (entry.session.archived, entry.session.control) == (False, "remote")
    registry.close()


async def test_a_terminal_attaching_takes_the_session_out_of_the_archive(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = load_archived(hub)

    await hub.attach_registered(
        Attachment(session_id="sess-1", cwd="/repo", pid=100, claude_version="2.1.267")
    )

    assert entry.session.archived is False
    assert entry.session.state == "idle"
    assert entry.session.control == "shared"
    assert published(frames)[-1]["archived"] is False
    await hub.close()
    registry.close()


async def test_a_turn_started_in_the_terminal_takes_a_shared_session_out_of_the_archive(
    tmp_path: Path,
) -> None:
    hub, frames, registry = build_hub(tmp_path)
    load_archived(hub)
    await hub.attach_registered(
        Attachment(session_id="sess-1", cwd="/repo", pid=100, claude_version="2.1.267")
    )
    entry = hub.entry("sess-1")
    result = await hub.archive({"session_id": "sess-1", "archived": True})
    assert result["session"]["archived"] is True

    await hub.shared.tick(entry, running=True)

    assert entry.session.archived is False
    assert entry.session.state == "running"
    assert published(frames)[-1]["archived"] is False
    await hub.close()
    registry.close()


async def test_a_message_held_for_a_shared_session_takes_it_out_of_the_archive(
    tmp_path: Path,
) -> None:
    hub, _, registry = build_hub(tmp_path)
    load_archived(hub)
    await hub.attach_registered(
        Attachment(session_id="sess-1", cwd="/repo", pid=100, claude_version="2.1.267")
    )
    entry = hub.entry("sess-1")
    await hub.shared.tick(entry, running=True)
    await hub.archive({"session_id": "sess-1", "archived": True})

    result = await hub.send({"id": "req-1", "session_id": "sess-1", "text": "when you can"})

    assert result["accepted"] == "queued"
    assert entry.session.archived is False
    await hub.close()
    registry.close()
