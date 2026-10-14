"""Session hub behaviour, driven with a fake agent runner."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.base import SessionRunner
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.errors import RcError
from rc_client.models import UNSET, AgentInfo, Choice, Command, Session, SpeedSetting
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry, SessionHub


class FakeRunner:
    agent = "claude"

    def __init__(self, channel: SessionChannel, *, steer: bool = False) -> None:
        self.channel = channel
        self.sent: list[str] = []
        self.sources: list[str] = []
        self.attachments: list[list[dict[str, Any]] | None] = []
        self.steered: list[str] = []
        self.steer_block_ids: list[str | None] = []
        self.interrupts = 0
        self.settings: list[tuple[Any, Any, Any, Any]] = []
        self.block_ids: list[str | None] = []
        self.commands_run: list[tuple[str, str | None, str]] = []
        self.closed = False
        self.shut_down = False
        self.order: list[str] = []
        self._busy = False
        self._steer = steer

    async def start(self) -> None:
        return None

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        self.block_ids.append(block_id)
        self.sent.append(text)
        self.sources.append(source)
        self.attachments.append(attachments)
        self._busy = True
        await self.channel.begin_turn(source)

    async def finish(self) -> None:
        self._busy = False
        await self.channel.end_turn("completed", 1)

    async def interrupt(self) -> bool:
        self.interrupts += 1
        self.order.append("interrupt")
        self._busy = False
        return True

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        return request_id == "known"

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        return request_id == "known"

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        self.settings.append((model, permission_mode, effort, speed))

    async def close(self) -> None:
        self.closed = True
        self.order.append("close")

    async def shutdown(self) -> None:
        self.shut_down = True
        self.order.append("shutdown")
        await self.close()

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def supports_steer(self) -> bool:
        return self._steer

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        if not self._steer:
            return False
        self.steered.append(text)
        self.steer_block_ids.append(block_id)
        return True

    async def commands(self) -> list[Command]:
        return [Command("compact", "Summarise the conversation", group="Built-in")]

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        if name != "compact":
            raise RcError("not_found", f"/{name} is not a command this session offers")
        self.commands_run.append((name, argument, block_id))
        await self.channel.emit(
            "user_message",
            block_id=block_id,
            text=f"/{name}" + (f" {argument}" if argument else ""),
            source="remote",
        )


def agent_info() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            permission_modes=[
                Choice("default", "Ask before edits"),
                Choice("acceptEdits", "Auto-accept edits"),
                Choice("plan", "Plan mode"),
            ],
            default_permission_mode="default",
            capabilities=["takeover", "interrupt", "queue", "history"],
        ),
        AgentInfo(agent="codex", available=False, capabilities=["interrupt", "queue", "steer"]),
    ]


def build_hub(tmp_path: Path) -> tuple[SessionHub, list[dict[str, Any]], Registry]:
    registry = Registry(tmp_path / "state.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(registry, publish, "dev-1", agent_info)
    return hub, frames, registry


def add_session(hub: SessionHub, *, steer: bool = False, control: str = "remote") -> SessionEntry:
    session = Session(
        session_id="sess-1",
        device_id="dev-1",
        agent="claude",
        cwd="/repo",
        state="idle",
        control=control,  # type: ignore[arg-type]
    )
    hub.registry.upsert_session(session)
    channel = SessionChannel(hub.registry, session, hub.publish)
    entry = SessionEntry(session=session, channel=channel)
    entry.runner = FakeRunner(channel, steer=steer)
    hub.entries[session.session_id] = entry
    return entry


async def test_send_to_an_idle_session_is_accepted_immediately(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    result = await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert result == {"accepted": "sent"}
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert runner.sent == ["go"]
    assert entry.session.title == "go"
    registry.close()


async def test_a_repeated_send_with_the_same_id_is_not_re_sent(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    first = await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go", "mode": "auto"})
    second = await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert first == second == {"accepted": "sent"}
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert runner.sent == ["go"]
    registry.close()


async def test_send_while_running_queues_and_launches_at_the_turn_end(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "first", "mode": "auto"})

    queued = await hub.send(
        {"id": "req-2", "session_id": "sess-1", "text": "second", "mode": "auto"}
    )
    assert queued == {"accepted": "queued", "queued_id": "req-2"}
    assert entry.session.queued == 1
    snapshot = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
    ][-1]
    assert snapshot["pending"][0]["text"] == "second"

    await runner.finish()
    await hub.drain_queue(entry)
    assert runner.sent == ["first", "second"]
    assert runner.sources == ["remote", "queue"]
    triggers = [
        frame["event"]["trigger"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "turn_started"
    ]
    assert triggers == ["remote", "queue"]
    assert entry.session.queued == 0
    registry.close()


async def test_auto_mode_steers_when_the_agent_supports_it(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub, steer=True)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "first", "mode": "auto"})
    result = await hub.send(
        {"id": "req-2", "session_id": "sess-1", "text": "also this", "mode": "auto"}
    )
    assert result == {"accepted": "steered"}
    assert runner.steered == ["also this"]
    registry.close()


async def test_interrupt_mode_stops_the_turn_before_sending(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "first", "mode": "auto"})
    result = await hub.send(
        {"id": "req-2", "session_id": "sess-1", "text": "stop and do this", "mode": "interrupt"}
    )
    assert result == {"accepted": "sent"}
    assert runner.interrupts == 1
    assert runner.sent == ["first", "stop and do this"]
    registry.close()


async def test_sending_to_a_terminal_controlled_session_is_a_conflict(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub, control="terminal")
    with pytest.raises(RcError) as caught:
        await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert caught.value.code == "conflict"
    assert "take over" in caught.value.message
    registry.close()


async def test_unknown_sessions_are_not_found(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    with pytest.raises(RcError) as caught:
        await hub.stop({"session_id": "nope"})
    assert caught.value.code == "not_found"
    registry.close()


async def test_queue_remove_drops_a_pending_message(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "first", "mode": "auto"})
    await hub.send({"id": "req-2", "session_id": "sess-1", "text": "second", "mode": "queue"})
    await hub.queue_remove({"session_id": "sess-1", "queued_id": "req-2"})
    assert entry.queue == []
    with pytest.raises(RcError):
        await hub.queue_remove({"session_id": "sess-1", "queued_id": "req-2"})
    registry.close()


async def test_approve_and_answer_report_stale_requests(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    assert (
        await hub.approve({"session_id": "sess-1", "request_id": "known", "option_id": "allow"})
        == {}
    )
    with pytest.raises(RcError) as caught:
        await hub.approve({"session_id": "sess-1", "request_id": "stale", "option_id": "allow"})
    assert caught.value.code == "not_found"
    with pytest.raises(RcError):
        await hub.answer({"session_id": "sess-1", "request_id": "stale", "answers": {}})
    registry.close()


async def test_set_options_updates_the_session_and_the_runner(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    result = await hub.set_options(
        {"session_id": "sess-1", "model": "opus", "permission_mode": "plan", "title": "Renamed"}
    )
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert runner.settings == [("opus", "plan", None, UNSET)]
    assert result["session"]["model"] == "opus"
    assert result["session"]["title"] == "Renamed"
    registry.close()


async def test_history_and_block_read_back_what_was_emitted(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await entry.channel.emit(
        "tool_call",
        block_id="toolu_1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="succeeded",
        output="x" * 40_000,
        started_at=1,
    )
    history = await hub.history({"session_id": "sess-1", "limit": 10})
    assert history["has_more"] is False
    assert len(history["events"][0]["output"]) < 40_000
    assert history["events"][0]["output_truncated"] is True

    block_id = history["events"][0]["block_id"]
    block = await hub.block({"session_id": "sess-1", "block_id": block_id})
    assert len(block["event"]["output"]) == 40_000
    with pytest.raises(RcError):
        await hub.block({"session_id": "sess-1", "block_id": "missing"})
    registry.close()


async def test_archive_stops_the_runner_and_delete_removes_the_session(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    result = await hub.archive({"session_id": "sess-1", "archived": True})
    assert result["session"]["archived"] is True
    assert runner.closed is True
    assert entry.session.control == "none"

    await hub.delete({"session_id": "sess-1"})
    assert "sess-1" not in hub.entries
    assert {"type": "session.removed", "session_id": "sess-1"} in frames
    registry.close()


async def test_takeover_is_refused_when_the_terminal_is_not_holding_the_session(
    tmp_path: Path,
) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as caught:
        await hub.takeover({"session_id": "sess-1"})
    assert caught.value.code == "conflict"
    registry.close()


async def test_create_rejects_a_missing_working_directory(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    with pytest.raises(RcError) as caught:
        await hub.create({"agent": "claude", "cwd": str(tmp_path / "nope")})
    assert caught.value.code == "bad_request"
    registry.close()


async def test_create_rejects_an_agent_that_is_not_installed(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    with pytest.raises(RcError) as caught:
        await hub.create({"agent": "codex", "cwd": str(tmp_path)})
    assert caught.value.code == "agent_unavailable"
    registry.close()


async def test_settings_outside_the_advertised_choices_are_refused(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "permission_mode": "nonsense"})
    assert caught.value.code == "bad_request"
    with pytest.raises(RcError) as caught:
        await hub.create({"agent": "claude", "cwd": str(tmp_path), "permission_mode": "nonsense"})
    assert caught.value.code == "bad_request"
    registry.close()


async def test_create_names_the_session_before_the_agent_starts(tmp_path: Path) -> None:
    """The reply must carry the final id: apps subscribe with what `session.create` returns."""
    hub, _, registry = build_hub(tmp_path)
    seen: dict[str, Any] = {}

    async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
        seen["session_id"] = entry.session.session_id
        seen["resume"] = resume
        return FakeRunner(entry.channel)

    hub._build_runner = build  # type: ignore[method-assign]
    result = await hub.create({"agent": "claude", "cwd": str(tmp_path)})

    session_id = result["session"]["session_id"]
    assert uuid.UUID(session_id).version == 4
    assert seen["session_id"] == session_id
    assert seen["resume"] is None
    assert session_id in hub.entries
    registry.close()


def test_claude_options_name_a_new_session_but_not_a_resumed_one(tmp_path: Path) -> None:
    """`--session-id` fixes a fresh session's id; on resume the transcript already has one."""
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    session = Session(session_id="fixed-id", device_id="dev-1", agent="claude", cwd=str(tmp_path))
    channel = SessionChannel(registry, session, publish)
    fresh = ClaudeRunner(channel, binary=None, cwd=str(tmp_path), session_id="fixed-id")
    assert fresh._options().session_id == "fixed-id"
    assert fresh._options().resume is None

    resumed = ClaudeRunner(
        channel, binary=None, cwd=str(tmp_path), session_id="fixed-id", resume="fixed-id"
    )
    assert resumed._options().resume == "fixed-id"
    assert resumed._options().session_id is None
    registry.close()


async def test_load_restores_persisted_sessions_as_resumable(tmp_path: Path) -> None:
    _, _, registry = build_hub(tmp_path)
    session = Session(
        session_id="old-1",
        device_id="dev-1",
        agent="claude",
        cwd="/repo",
        state="running",
        control="remote",
        queued=3,
    )
    registry.upsert_session(session)

    fresh, _, _ = build_hub(tmp_path)
    fresh.registry = registry
    fresh.load()
    restored = fresh.entries["old-1"].session
    assert restored.state == "idle"
    assert restored.control == "none"
    assert restored.queued == 0
    assert restored.turn is None
    registry.close()


async def test_rekeying_moves_the_session_to_the_agents_real_id(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    session = Session(
        session_id="pending-1", device_id="dev-1", agent="claude", cwd="/repo", state="starting"
    )
    registry.upsert_session(session)
    entry = SessionEntry(session=session, channel=SessionChannel(registry, session, hub.publish))
    hub.entries["pending-1"] = entry

    await hub.rekey(entry, "real-1")
    assert "pending-1" not in hub.entries
    assert hub.entries["real-1"].session.session_id == "real-1"
    assert {"type": "session.removed", "session_id": "pending-1"} in frames
    registry.close()


def test_runner_protocol_is_satisfied_by_the_fake(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s", device_id="d", agent="claude", cwd="/repo")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    runner = FakeRunner(SessionChannel(registry, session, publish))
    assert isinstance(runner, SessionRunner)
    registry.close()


# ------------------------------------------- A12: the app's id is the bubble

FIRST_REQUEST = "3f1c9d2a-6b48-4f2e-9a77-1c5be0d4a911"
SECOND_REQUEST = "b72e5d18-0c3a-4d6f-8e21-9fd4c7a35b60"


async def test_the_request_id_is_the_block_the_message_lands_on(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": FIRST_REQUEST, "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert runner.block_ids == [FIRST_REQUEST]
    registry.close()


async def test_a_retry_of_the_same_send_lands_on_the_same_block(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    for _ in range(2):
        await hub.send({"id": FIRST_REQUEST, "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert runner.block_ids == [FIRST_REQUEST], "the duplicate was never sent again"
    registry.close()


async def test_a_queued_message_keeps_its_id_from_the_queue_to_the_bubble(
    tmp_path: Path,
) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": FIRST_REQUEST, "session_id": "sess-1", "text": "first", "mode": "auto"})
    await hub.send({"id": SECOND_REQUEST, "session_id": "sess-1", "text": "second", "mode": "auto"})
    snapshot = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
    ][-1]
    assert snapshot["pending"][0]["id"] == SECOND_REQUEST

    await runner.finish()
    await hub.drain_queue(entry)
    assert runner.block_ids == [FIRST_REQUEST, SECOND_REQUEST]
    registry.close()


async def test_a_message_the_device_replays_keeps_an_id_of_its_own(tmp_path: Path) -> None:
    """A queued message that arrived without a request id is the device's own."""
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": FIRST_REQUEST, "session_id": "sess-1", "text": "first", "mode": "auto"})
    queued = await hub.send({"session_id": "sess-1", "text": "second", "mode": "auto"})
    minted = str(queued["queued_id"])
    assert minted and minted != FIRST_REQUEST

    await runner.finish()
    await hub.drain_queue(entry)
    assert runner.block_ids == [FIRST_REQUEST, minted]
    registry.close()


async def test_a_steered_message_carries_the_request_id_too(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub, steer=True)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": FIRST_REQUEST, "session_id": "sess-1", "text": "first", "mode": "auto"})
    result = await hub.send(
        {"id": SECOND_REQUEST, "session_id": "sess-1", "text": "also this", "mode": "auto"}
    )
    assert result == {"accepted": "steered"}
    assert runner.steer_block_ids == [SECOND_REQUEST]
    registry.close()
