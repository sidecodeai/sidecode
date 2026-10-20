"""Working means all of it: a Claude session with subagents under way is green.

The rule of `docs/DESIGN.md` § "Working means all of it":
the turn does not end, and the dot does not turn amber, while any work the
session started is still running. Claude Code says nothing about a subagent's
state, so the device reads the last row of each subagent's transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import adapter as claude_adapter
from rc_client.agents.claude import subagents, transcripts
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.agents.claude.subagents import SubagentWatch, transcript_ended
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.grok import sessions as grok_sessions
from rc_client.models import AgentInfo, Choice, Session, now_ms
from rc_client.registry import Registry
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.attach import Attachment
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.helpers import drained
from tests.test_app_defects import StubSDK
from tests.test_hub import add_session, build_hub

# ------------------------------------------------------------------- the rule


def assistant(stop_reason: str | None, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"id": "msg_1", "content": [{"type": "text", "text": "ok"}]}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {"type": "assistant", "uuid": "a1", "message": message, **extra}


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", "max_tokens"])
def test_an_assistant_row_that_ended_a_turn_ends_the_subagent(stop_reason: str) -> None:
    assert transcript_ended(assistant(stop_reason)) is True


def test_the_usage_limit_row_ends_the_subagent() -> None:
    """The shape the CLI writes on a 429: a stop sequence flagged as an API error."""
    assert transcript_ended(assistant("stop_sequence", isApiErrorMessage=True)) is True
    assert transcript_ended({"type": "user", "isApiErrorMessage": True}) is True


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(assistant("tool_use"), id="a tool call is still pending"),
        pytest.param(assistant(None), id="a streamed block whose message never completed"),
        pytest.param(
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            id="the tool's result came back",
        ),
        pytest.param({"type": "attachment", "attachment": {"type": "model"}}, id="an attachment"),
        pytest.param({"type": "assistant", "message": "not an object"}, id="a malformed message"),
        pytest.param(None, id="an empty or unreadable file"),
        pytest.param("not a row", id="something that is not a row at all"),
    ],
)
def test_everything_else_is_work_in_progress(row: Any) -> None:
    """A34's rule is the parent's, not a subagent's.

    The parent's transcript is read as it grows, so a missing `stop_reason` ends
    its turn; these files are read from the end long afterwards, and a large
    share of them stop on exactly such a row.
    """
    assert transcript_ended(row) is False


# ---------------------------------------------------------------- the watcher


def write_agent(directory: Path, name: str, rows: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def test_a_subagent_directory_that_does_not_exist_is_not_working(tmp_path: Path) -> None:
    watch = SubagentWatch(directory=tmp_path / "nowhere")
    assert watch.working() is False


def test_the_watch_hangs_off_the_session_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "proj" / "sess-1.jsonl"
    assert subagents.subagents_dir(transcript) == tmp_path / "proj" / "sess-1" / "subagents"


def test_a_running_subagent_is_named_and_a_finished_one_is_not(tmp_path: Path) -> None:
    write_agent(tmp_path, "agent-running", [assistant("tool_use")])
    write_agent(tmp_path, "agent-done", [assistant("end_turn")])
    watch = SubagentWatch(directory=tmp_path)

    assert watch.working_agents() == ["agent-running"]
    assert watch.working() is True


def test_an_empty_transcript_is_a_subagent_that_has_not_spoken_yet(tmp_path: Path) -> None:
    (tmp_path / "agent-new.jsonl").write_bytes(b"")
    assert SubagentWatch(directory=tmp_path).working_agents() == ["agent-new"]


def test_a_tail_that_does_not_parse_is_read_as_still_working(tmp_path: Path) -> None:
    """The safe direction: a session stays green and the stale bound ends it."""
    (tmp_path / "agent-torn.jsonl").write_bytes(b"\x00\x01 not json at all")
    assert SubagentWatch(directory=tmp_path).working_agents() == ["agent-torn"]


def test_a_transcript_nothing_has_written_to_for_half_an_hour_is_abandoned(
    tmp_path: Path,
) -> None:
    """Nothing marks a killed subagent, so nothing may hold a session green for good."""
    path = write_agent(tmp_path, "agent-killed", [assistant("tool_use")])
    old = time.time() - subagents.SUBAGENT_STALE_S - 60
    os.utime(path, (old, old))

    assert SubagentWatch(directory=tmp_path).working_agents() == []


def test_a_finished_transcript_is_read_once_and_then_only_stat_ed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that spawned a hundred subagents is asked on every tail interval."""
    write_agent(tmp_path, "agent-done", [assistant("end_turn")])
    write_agent(tmp_path, "agent-running", [assistant("tool_use")])
    reads: list[str] = []
    real = subagents.read_last_row

    def counted(path: Any, tail_bytes: int = subagents.TAIL_BYTES) -> Any:
        reads.append(Path(path).stem)
        return real(path, tail_bytes)

    monkeypatch.setattr(subagents, "read_last_row", counted)
    watch = SubagentWatch(directory=tmp_path)

    assert watch.working_agents() == ["agent-running"]
    assert sorted(reads) == ["agent-done", "agent-running"]
    reads.clear()
    assert watch.working_agents() == ["agent-running"]
    assert reads == ["agent-running"]


def test_a_removed_transcript_is_forgotten(tmp_path: Path) -> None:
    path = write_agent(tmp_path, "agent-done", [assistant("end_turn")])
    watch = SubagentWatch(directory=tmp_path)
    watch.working_agents()
    path.unlink()

    assert watch.working_agents() == []
    assert watch._ended == {}


def test_only_a_long_last_row_defeats_the_tail_window(tmp_path: Path) -> None:
    """Reading the end of the file is enough however much came before it."""
    filler = [assistant("tool_use") for _ in range(200)]
    write_agent(tmp_path, "agent-long", [*filler, assistant("end_turn")])

    assert SubagentWatch(directory=tmp_path).working_agents() == []


# ----------------------------------------------------- the mirror and sharing


def agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["takeover", "interrupt", "queue", "history"],
            attach="channel",
            attach_ready=True,
            shared_interrupt=False,
        )
    ]


def user_row(text: str, uuid: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}, **extra}


TASK_NOTIFICATION = (
    "<task-notification>\n"
    "<summary>review-pr-1 finished</summary>\n"
    "<result>Two findings, both filed.</result>\n"
    "</task-notification>"
)


class FakeAttachment(Attachment):
    """An attachment whose injections reach a CLI that is not there."""

    def __init__(self, session_id: str, cwd: str) -> None:
        super().__init__(session_id=session_id, cwd=cwd, pid=4242, claude_version="2.1.267")
        self.injected: list[tuple[str, str]] = []

    async def inject(self, message_id: str, text: str) -> bool:
        self.injected.append((message_id, text))
        return True

    def detach(self) -> None:
        return None


class MirrorHarness:
    """One attached Claude session, its transcript on disk and its subagents."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.frames: list[dict[str, Any]] = []
        self.cwd = str(tmp_path / "repo")
        self.project = tmp_path / "projects" / "repo"
        self.project.mkdir(parents=True)
        self.transcript = self.project / "sess-1.jsonl"
        self.subagents = self.project / "sess-1" / "subagents"
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", agents)
        self.hub.load()
        self.write([user_row("hello", "u0")])
        found = [
            transcripts.TranscriptInfo(
                session_id="sess-1",
                path=str(self.transcript),
                cwd=self.cwd,
                size=self.transcript.stat().st_size,
                mtime=self.transcript.stat().st_mtime,
            )
        ]
        monkeypatch.setattr(transcripts, "discover", lambda *args: found)
        monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])
        monkeypatch.setattr(grok_sessions, "discover", lambda *args: [])

        async def no_holders() -> Any:
            from rc_client.agents.claude.holders import HolderScan

            return HolderScan(holders=[], complete=True)

        monkeypatch.setattr(mirror_module, "scan_holders", no_holders)
        self.mirror = MirrorService(self.hub)
        # The mirror starts where the file already is, as it does in life.
        self.registry.set_kv(self.mirror._offset_key("sess-1"), str(found[0].size))

    def write(self, rows: list[dict[str, Any]]) -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def subagent(self, name: str, rows: list[dict[str, Any]]) -> Path:
        return write_agent(self.subagents, name, rows)

    async def attach(self) -> Any:
        self.attachment = FakeAttachment(session_id="sess-1", cwd=self.cwd)
        await self.hub.attach_registered(self.attachment)
        await self.mirror.scan_once()
        return self.hub.entry("sess-1")

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
async def mirrored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    built = MirrorHarness(tmp_path, monkeypatch)
    yield built
    await built.hub.close()
    built.close()


async def test_a_turn_stays_open_while_a_subagent_the_session_started_works(
    mirrored: MirrorHarness,
) -> None:
    """The reported defect: the row read "Turn finished" while the work ran on."""
    entry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    await mirrored.mirror.tail_once()
    assert entry.session.state == "running"

    # The parent hands the work to a background subagent and ends its own turn.
    mirrored.subagent("agent-1", [assistant("tool_use")])
    mirrored.write([assistant("end_turn")])
    await mirrored.mirror.tail_once()

    assert entry.session.state == "running"
    assert entry.session.turn is not None
    assert mirrored.events("turn_completed") == []

    # Nothing is written to the parent's transcript when the last one finishes,
    # so the tail interval has to ask the subagents themselves.
    write_agent(mirrored.subagents, "agent-1", [assistant("end_turn")])
    await mirrored.mirror.tail_once()

    assert entry.session.state == "idle"
    assert entry.session.turn is None
    completed = mirrored.events("turn_completed")
    assert len(completed) == 1
    assert completed[0]["stop_reason"] == "completed"

    # And the turn ends once, not once per tail.
    await mirrored.mirror.tail_once()
    assert len(mirrored.events("turn_completed")) == 1


async def test_the_task_notification_of_a_finished_subagent_opens_no_second_turn(
    mirrored: MirrorHarness,
) -> None:
    """One turn, from the person's prompt to the last subagent's end."""
    entry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    mirrored.subagent("agent-1", [assistant("tool_use")])
    await mirrored.mirror.tail_once()
    mirrored.write([assistant("end_turn")])
    await mirrored.mirror.tail_once()

    # The CLI files the notification as a user turn nobody typed (A30/A34).
    mirrored.write(
        [user_row(TASK_NOTIFICATION, "u2", promptSource="system", origin={"kind": "task"})]
    )
    write_agent(mirrored.subagents, "agent-1", [assistant("end_turn")])
    await mirrored.mirror.tail_once()

    assert len(mirrored.events("turn_started")) == 1
    assert mirrored.events("turn_started")[0]["trigger"] == "terminal"
    # The notification reopened the parent's own turn, so it is still working.
    assert entry.session.state == "running"
    assert mirrored.events("turn_completed") == []

    mirrored.write([assistant("end_turn")])
    await mirrored.mirror.tail_once()

    assert entry.session.state == "idle"
    assert len(mirrored.events("turn_started")) == 1
    assert len(mirrored.events("turn_completed")) == 1


async def test_a_held_message_waits_for_the_turn_and_never_for_the_subagents(
    mirrored: MirrorHarness,
) -> None:
    """A51: the CLI takes a message between its subagents' steps, so nothing waits on them.

    The session still shows as working while they are, because working means
    all of it; only what decides injecting changed.
    """
    entry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    mirrored.subagent("agent-1", [assistant("tool_use")])
    await mirrored.mirror.tail_once()
    result = await mirrored.hub.send({"id": "req-1", "session_id": "sess-1", "text": "later"})
    assert result == {"accepted": "queued", "queued_id": "req-1"}

    mirrored.write([assistant("end_turn")])
    await mirrored.mirror.tail_once()
    await drained(entry)
    assert entry.queue == []
    assert [text for _, text in mirrored.attachment.injected] == ["later"]
    assert entry.session.state == "running", "the subagent is still at work"

    # Idle in its own turn, with the subagent still working: a message goes in at once.
    message_id, said = mirrored.attachment.injected[-1]
    mirrored.write([channel_echo(message_id, said), assistant("end_turn")])
    await mirrored.mirror.tail_once()
    result = await mirrored.hub.send({"id": "req-2", "session_id": "sess-1", "text": "now"})
    assert result == {"accepted": "sent"}
    assert entry.session.state == "running"


def channel_echo(message_id: str, text: str) -> dict[str, Any]:
    """The row the CLI files for a message the channel delivered."""
    return {
        "type": "user",
        "uuid": f"echo-{message_id}",
        "isMeta": True,
        "origin": {"kind": "channel", "server": "rc"},
        "message": {
            "role": "user",
            "content": f'<channel source="rc" origin="app" message_id="{message_id}">\n'
            f"{text}\n</channel>",
        },
    }


async def test_a_read_only_mirror_is_running_while_its_subagents_are(
    mirrored: MirrorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is the same whether the device drives the session or watches it."""
    from rc_client.agents.claude.holders import Holder, HolderScan

    entry = await mirrored.attach()
    await mirrored.hub.attach_closed(mirrored.hub.entry("sess-1").shared.attachment)  # type: ignore[union-attr]

    async def one_holder() -> HolderScan:
        return HolderScan(
            holders=[
                Holder(
                    pid=4242,
                    identity=(4242, "a"),
                    session_id="sess-1",
                    cwd=mirrored.cwd,
                    attachable=False,
                )
            ],
            complete=True,
        )

    monkeypatch.setattr(mirror_module, "scan_holders", one_holder)
    mirrored.write([user_row("go", "u1"), assistant("end_turn")])
    mirrored.subagent("agent-1", [assistant("tool_use")])
    await mirrored.mirror.tail_once()
    await mirrored.mirror.scan_once()

    assert entry.session.control == "terminal"
    assert entry.session.state == "running"

    write_agent(mirrored.subagents, "agent-1", [assistant("end_turn")])
    await mirrored.mirror.tail_once()

    assert entry.session.state == "readonly"


# --------------------------------------------------------------- the adapter


class FakeClient:
    """Enough of the SDK client for a turn that never reached one."""

    def __init__(self) -> None:
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def get_context_usage(self) -> dict[str, Any]:
        return {}


class DrivenHarness:
    """A session this device drives, with its transcript where the CLI files it."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.frames: list[dict[str, Any]] = []
        projects = tmp_path / "projects"
        self.project = projects / "repo"
        self.project.mkdir(parents=True)
        (self.project / "sess-1.jsonl").write_bytes(b"")
        self.subagents = self.project / "sess-1" / "subagents"
        monkeypatch.setattr(transcripts, "PROJECTS_DIR", projects)
        monkeypatch.setattr(claude_adapter, "SUBAGENT_POLL_S", 0.01)
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        session = Session(
            session_id="sess-1",
            device_id="dev-1",
            agent="claude",
            cwd=str(tmp_path),
            origin="remote",
            control="remote",
        )
        self.registry.upsert_session(session)
        self.channel = SessionChannel(self.registry, session, publish)
        self.runner = ClaudeRunner(
            self.channel, binary=None, cwd=str(tmp_path), session_id="sess-1"
        )
        self.client = FakeClient()
        self.runner._client = self.client  # type: ignore[assignment]

    async def open_turn(self) -> None:
        self.runner._turn_done.clear()
        self.runner._turn_started_at = now_ms()
        await self.channel.begin_turn("remote")

    def subagent(self, name: str, rows: list[dict[str, Any]]) -> Path:
        return write_agent(self.subagents, name, rows)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
def driven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    built = DrivenHarness(tmp_path, monkeypatch)
    yield built
    built.close()


RESULT = {"stop_reason": "completed", "duration_ms": 1234, "usage": {"input_tokens": 5}}


async def test_the_sdk_result_does_not_end_a_turn_whose_subagents_are_working(
    driven: DrivenHarness,
) -> None:
    driven.subagent("agent-1", [assistant("tool_use")])
    await driven.open_turn()

    await driven.runner._finish_turn(dict(RESULT))

    assert driven.events("turn_completed") == []
    assert driven.runner.busy is True
    assert driven.channel.session.state == "running"

    hold = driven.runner._hold
    assert hold is not None
    write_agent(driven.subagents, "agent-1", [assistant("end_turn")])
    await asyncio.wait_for(hold, timeout=5)

    completed = driven.events("turn_completed")
    assert len(completed) == 1
    assert completed[0]["stop_reason"] == "completed"
    # The person watched all of it, so the turn reports all of it and not the
    # CLI's own 1234 ms.
    assert completed[0]["duration_ms"] != 1234
    assert driven.runner.busy is False


async def test_a_second_result_while_the_turn_is_held_is_the_same_turn(
    driven: DrivenHarness,
) -> None:
    """The A34 continuation the CLI runs for a subagent's message ends nothing."""
    driven.subagent("agent-1", [assistant("tool_use")])
    await driven.open_turn()
    await driven.runner._finish_turn(dict(RESULT))
    hold = driven.runner._hold
    assert hold is not None

    await driven.runner._finish_turn({**RESULT, "stop_reason": "error"})
    assert driven.events("turn_completed") == []
    assert driven.runner._hold is hold

    write_agent(driven.subagents, "agent-1", [assistant("end_turn")])
    await asyncio.wait_for(hold, timeout=5)

    completed = driven.events("turn_completed")
    assert len(completed) == 1
    # The last word on how the turn ended is the one that is published.
    assert completed[0]["stop_reason"] == "error"
    assert driven.events("turn_started") == [driven.events("turn_started")[0]]


async def test_a_turn_with_no_subagents_ends_on_the_result_as_it_always_did(
    driven: DrivenHarness,
) -> None:
    await driven.open_turn()

    await driven.runner._finish_turn(dict(RESULT))

    completed = driven.events("turn_completed")
    assert len(completed) == 1
    assert completed[0]["duration_ms"] == 1234
    assert driven.runner._hold is None
    assert driven.runner.busy is False


async def test_interrupting_a_held_turn_ends_it_as_interrupted(
    driven: DrivenHarness,
) -> None:
    """There is no turn left on the CLI to interrupt; the device's hold is what ends."""
    driven.subagent("agent-1", [assistant("tool_use")])
    await driven.open_turn()
    await driven.runner._finish_turn(dict(RESULT))
    hold = driven.runner._hold
    assert hold is not None

    assert await driven.runner.interrupt() is True

    completed = driven.events("turn_completed")
    assert len(completed) == 1
    assert completed[0]["stop_reason"] == "interrupted"
    assert driven.runner._hold is None
    assert driven.runner.busy is False
    assert hold.cancelled()
    assert driven.client.interrupts == 0


async def test_a_turn_the_person_interrupted_is_never_held(driven: DrivenHarness) -> None:
    """They said stop, and `interrupt` is already waiting on the drain."""
    driven.subagent("agent-1", [assistant("tool_use")])
    await driven.open_turn()
    driven.runner._interrupting = True

    await driven.runner._finish_turn({**RESULT, "stop_reason": "interrupted"})

    assert driven.runner._hold is None
    assert [event["stop_reason"] for event in driven.events("turn_completed")] == ["interrupted"]


async def test_interrupt_and_send_on_a_held_turn_sends_the_new_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping a held turn ends it inline, inside the send that holds the lock.

    The turn's end only schedules the drain, so the send goes on to deliver
    its own message, and the one already held waits for that turn (A43).
    """
    projects = tmp_path / "projects"
    (projects / "repo").mkdir(parents=True)
    (projects / "repo" / "sess-1.jsonl").write_bytes(b"")
    write_agent(projects / "repo" / "sess-1" / "subagents", "agent-1", [assistant("tool_use")])
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", projects)
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = await hub._build_runner(entry, hub.agent_info("claude"), resume=None)
    assert isinstance(runner, ClaudeRunner)
    sdk = StubSDK()
    runner._client = sdk  # type: ignore[assignment]
    entry.runner = runner
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"})
    await runner._finish_turn(dict(RESULT))
    assert runner._hold is not None
    await hub.send({"id": "req-2", "session_id": "sess-1", "text": "later", "mode": "queue"})

    result = await asyncio.wait_for(
        hub.send({"id": "req-3", "session_id": "sess-1", "text": "now", "mode": "interrupt"}),
        timeout=2,
    )

    assert result == {"accepted": "sent"}
    assert sdk.prompts == ["go", "now"]
    await drained(entry)
    assert [item["id"] for item in entry.queue] == ["req-2"]
    await entry.channel.close()
    registry.close()
