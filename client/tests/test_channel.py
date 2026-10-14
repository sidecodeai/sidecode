"""Session channel: seq assignment, delta coalescing and state transitions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from rc_client.ids import block_uuid
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.helpers import event_validator


class Recorder:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def events(self) -> list[dict[str, Any]]:
        return [frame["event"] for frame in self.frames if frame.get("type") == "session.event"]


def build(tmp_path: Path) -> tuple[SessionChannel, Recorder, Registry]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/tmp")
    registry.upsert_session(session)
    recorder = Recorder()
    return SessionChannel(registry, session, recorder), recorder, registry


async def test_emit_assigns_increasing_seq_and_publishes(tmp_path: Path) -> None:
    channel, recorder, registry = build(tmp_path)
    await channel.emit("notice", level="info", text="one")
    await channel.emit("notice", level="info", text="two")
    events = recorder.events()
    assert [event["seq"] for event in events] == [1, 2]
    assert events[0]["kind"] == "notice"
    assert channel.session.last_seq == 2
    registry.close()


async def test_deltas_are_coalesced_per_block(tmp_path: Path) -> None:
    channel, recorder, registry = build(tmp_path)
    channel.start()
    for piece in ("a", "b", "c"):
        await channel.emit_delta("assistant_text", "blk", piece)
    await asyncio.sleep(0.2)
    await channel.close()
    deltas = [event for event in recorder.events() if event.get("delta")]
    assert len(deltas) == 1
    assert deltas[0]["delta"] == "abc"
    assert deltas[0]["block_id"] == block_uuid("blk")
    registry.close()


async def test_a_final_event_flushes_its_pending_deltas_first(tmp_path: Path) -> None:
    channel, recorder, registry = build(tmp_path)
    await channel.emit_delta("assistant_text", "blk", "partial")
    await channel.emit("assistant_text", block_id="blk", text="partial done", done=True)
    events = recorder.events()
    assert [event.get("delta") or event.get("text") for event in events] == [
        "partial",
        "partial done",
    ]
    assert events[0]["seq"] < events[1]["seq"]
    await channel.close()
    registry.close()


async def test_only_final_text_reaches_history(tmp_path: Path) -> None:
    channel, _, registry = build(tmp_path)
    await channel.emit_delta("assistant_text", "blk", "part")
    await channel.flush_all()
    await channel.emit("assistant_text", block_id="blk", text="part whole", done=True)
    events, _ = registry.history("s1")
    assert len(events) == 1
    assert events[0]["text"] == "part whole"
    await channel.close()
    registry.close()


async def test_state_change_emits_status_and_a_session_summary(tmp_path: Path) -> None:
    channel, recorder, registry = build(tmp_path)
    await channel.set_state("running")
    await channel.set_state("running")
    kinds = [frame.get("type") for frame in recorder.frames]
    assert kinds == ["session.event", "session.updated"]
    assert recorder.events()[0] == {
        "seq": 1,
        "ts": recorder.events()[0]["ts"],
        "kind": "status",
        "state": "running",
    }
    registry.close()


async def test_turn_markers_carry_one_turn_id(tmp_path: Path) -> None:
    channel, recorder, registry = build(tmp_path)
    turn_id = await channel.begin_turn("remote")
    await channel.end_turn("completed", 120, {"total_tokens": 10})
    events = recorder.events()
    started = next(event for event in events if event["kind"] == "turn_started")
    completed = next(event for event in events if event["kind"] == "turn_completed")
    assert started["turn_id"] == turn_id == completed["turn_id"]
    assert completed["usage"]["total_tokens"] == 10
    assert channel.session.state == "idle"
    assert channel.session.turn is None
    registry.close()


async def test_emitted_events_match_the_frozen_schema(tmp_path: Path) -> None:
    validator = event_validator()
    channel, recorder, registry = build(tmp_path)
    await channel.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="pytest -q",
        status="running",
        input={"command": "pytest -q"},
        started_at=1,
    )
    await channel.emit("user_message", block_id="u1", text="go", source="remote")
    await channel.publish_todos([{"id": "1", "text": "do it", "status": "pending"}])
    await channel.begin_turn("remote")
    await channel.end_turn("completed", 5)
    if validator is not None:
        for event in recorder.events():
            validator.validate(event)
    registry.close()
