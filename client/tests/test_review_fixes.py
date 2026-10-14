"""Regression tests for the review findings and amendments A7 and A8."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude.holders import Holder, scan_holders
from rc_client.agents.codex.models import parse_catalog
from rc_client.agents.codex.translate import normalise
from rc_client.errors import RcError
from rc_client.events import MAX_EVENT_BYTES, bound_event
from rc_client.gateway import MAX_FRAME_BYTES, SILENCE_TIMEOUT
from rc_client.models import Session
from rc_client.procscan import Proc, descendants
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry
from rc_client.tailing import READ_CHUNK, FileTail
from tests.helpers import event_validator
from tests.test_hub import FakeRunner, add_session, build_hub

# ------------------------------------------------------------------ finding 2


def test_the_device_socket_accepts_a_contract_sized_attachment_frame() -> None:
    """PROTOCOL section 5 allows 8 attachments of 6 MiB, base64 encoded."""
    largest_send = 8 * 6 * 1024 * 1024 * 4 // 3
    assert largest_send < MAX_FRAME_BYTES


# ------------------------------------------------------------------ finding 3


async def test_streaming_deltas_carry_done_false_and_match_the_schema(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/tmp")
    registry.upsert_session(session)
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    channel = SessionChannel(registry, session, publish)
    await channel.emit_delta("assistant_text", "blk", "partial")
    await channel.emit_delta("thinking", "think", "why")
    await channel.flush_all()
    await channel.close()

    events = [frame["event"] for frame in frames]
    assert [event["done"] for event in events] == [False, False]
    validator = event_validator()
    if validator is not None:
        for event in events:
            validator.validate(event)
    registry.close()


# ------------------------------------------------------------------ finding 5


async def test_an_unadvertised_option_id_is_treated_as_a_refusal(tmp_path: Path) -> None:
    from rc_client.agents.claude.adapter import ClaudeRunner

    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        return None

    channel = SessionChannel(registry, session, publish)
    runner = ClaudeRunner(channel, binary=None, cwd=str(tmp_path))

    async def answer_with_a_codex_id() -> None:
        await asyncio.sleep(0.05)
        pending = next(iter(runner._pending))
        await runner.approve(pending, "denied", None)

    task = asyncio.create_task(answer_with_a_codex_id())
    result = await runner._ask_approval("Bash", {"command": "rm -rf /"}, _context())
    await task
    assert result.behavior == "deny"
    await channel.close()
    registry.close()


def _context() -> Any:
    from claude_agent_sdk import ToolPermissionContext

    return ToolPermissionContext(suggestions=[])


# ------------------------------------------------------------------ finding 4


async def test_a_deleted_session_stops_being_mirrored(tmp_path: Path) -> None:
    from rc_client.sessions.mirror import ClaudeMirror, MirrorService

    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = None
    entry.session.origin = "terminal"
    mirror = MirrorService(hub)
    mirror._claude["sess-1"] = ClaudeMirror(tailer=_tailer(tmp_path))

    assert mirror._live_entry("sess-1", mirror._claude) is entry
    hub.entries.pop("sess-1")
    assert mirror._live_entry("sess-1", mirror._claude) is None
    assert "sess-1" not in mirror._claude
    registry.close()


async def test_a_rekeyed_session_is_re_resolved_not_held_by_object(tmp_path: Path) -> None:
    from rc_client.sessions.mirror import ClaudeMirror, MirrorService

    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = None
    entry.session.origin = "terminal"
    mirror = MirrorService(hub)
    mirror._claude["sess-1"] = ClaudeMirror(tailer=_tailer(tmp_path))

    replacement = SessionEntry(session=entry.session, channel=entry.channel)
    hub.entries["sess-1"] = replacement
    assert mirror._live_entry("sess-1", mirror._claude) is replacement

    replacement.runner = FakeRunner(entry.channel)
    assert mirror._live_entry("sess-1", mirror._claude) is None
    assert "sess-1" not in mirror._claude
    registry.close()


def _tailer(tmp_path: Path) -> Any:
    from rc_client.agents.claude.transcripts import TranscriptTailer

    path = tmp_path / "t.jsonl"
    path.write_text("")
    return TranscriptTailer(path=str(path), cwd=str(tmp_path))


# ------------------------------------------------------------------ finding 8


def test_the_daemon_subtree_is_never_taken_for_a_terminal_holder() -> None:
    procs = [
        Proc(pid=100, ppid=1, start="a", command="rc-client run"),
        Proc(pid=101, ppid=100, start="b", command="/usr/bin/node claude"),
        Proc(pid=102, ppid=101, start="c", command="claude --resume x"),
        Proc(pid=200, ppid=1, start="d", command="claude"),
    ]
    assert descendants(procs, 100) == {100, 101, 102}
    assert 200 not in descendants(procs, 100)


async def test_holder_scan_excludes_our_own_children(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from rc_client import procscan
    from rc_client.agents.claude import holders

    mine = os.getpid()
    procs = [
        Proc(pid=mine, ppid=1, start="a", command="rc-client run"),
        Proc(pid=mine + 1, ppid=mine, start="b", command="/Users/me/.local/bin/claude"),
        Proc(pid=mine + 2, ppid=1, start="c", command="/Users/me/.local/bin/claude --resume abc"),
    ]

    async def fake_scan() -> Any:
        return procscan.Scan(procs=procs, complete=True)

    async def fake_cwds(pids: list[int]) -> tuple[dict[int, str], bool]:
        return {pid: "/repo" for pid in pids}, True

    monkeypatch.setattr(holders, "scan_processes", fake_scan)
    monkeypatch.setattr(holders, "process_cwds", fake_cwds)
    scan = await scan_holders()
    assert [holder.pid for holder in scan.holders] == [mine + 2]


# ------------------------------------------------------------------ finding 9


def test_bound_event_fits_the_64_kib_frame_the_gateway_accepts() -> None:
    event = {
        "seq": 1,
        "ts": 0,
        "kind": "tool_call",
        "block_id": "b1",
        "tool": "Edit",
        "tool_kind": "edit",
        "title": "a.py",
        "status": "succeeded",
        "started_at": 0,
        "input": {"file_path": "a.py", "old_string": 'x"\n' * 4000},
        "output": 'quoted "output"\n' * 4000,
        "diff": {"path": "a.py", "additions": 1, "deletions": 1, "patch": '+"\n' * 20000},
    }
    bounded = bound_event(event)
    assert len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) <= MAX_EVENT_BYTES
    assert bounded["output_truncated"] is True


# ----------------------------------------------------------------- finding 10


def test_clamp_effort_survives_an_effort_id_codex_adds_later() -> None:
    catalog = parse_catalog(
        [
            {
                "id": "future",
                "displayName": "Future",
                "isDefault": True,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "hyper"},
                ],
            }
        ]
    )
    assert catalog.clamp_effort("future", "max") in {"low", "hyper"}
    assert catalog.clamp_effort("future", "hyper") == "hyper"


# ----------------------------------------------------------------- finding 11


def test_normalise_leaves_file_paths_and_tool_arguments_alone() -> None:
    payload = {
        "item_id": "x",
        "changes": {"/tmp/my_file.py": {"unified_diff": "@@"}},
        "arguments": {"file_path": "/tmp/my_file.py"},
    }
    result = normalise(payload)
    assert result["itemId"] == "x"
    assert "/tmp/my_file.py" in result["changes"]
    assert result["arguments"]["file_path"] == "/tmp/my_file.py"


# ----------------------------------------------------------------- finding 16


def test_a_damaged_session_row_is_skipped_not_fatal(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    registry.upsert_session(Session(session_id="good", device_id="d", agent="claude", cwd="/tmp"))
    registry.next_seq("orphan")
    assert [session.session_id for session in registry.load_sessions()] == ["good"]
    registry.close()


# ----------------------------------------------------------------- finding 19


async def test_an_explicit_queue_request_queues_even_when_idle(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    result = await hub.send(
        {"id": "req-1", "session_id": "sess-1", "text": "later please", "mode": "queue"}
    )
    assert result["accepted"] == "queued"
    assert runner.sent == ["later please"]
    assert entry.queue == []
    registry.close()


# ----------------------------------------------------------------- finding 18


async def test_queued_messages_keep_their_attachments(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "first", "mode": "auto"})
    attachment = {"name": "shot.png", "mime": "image/png", "data_base64": "aGk="}
    await hub.send(
        {
            "id": "req-2",
            "session_id": "sess-1",
            "text": "look",
            "mode": "auto",
            "attachments": [attachment],
        }
    )
    assert entry.queue[0]["attachments"] == [attachment]
    snapshot = hub.queue_snapshot(entry)
    # The files stay on the device; only how many there are goes out (A43).
    assert snapshot == [
        {"id": "req-2", "text": "look", "ts": entry.queue[0]["ts"], "attachments": 1}
    ]

    await runner.finish()
    await hub.drain_queue(entry)
    assert runner.attachments[-1] == [attachment]
    registry.close()


# ----------------------------------------------------------------- finding 21


async def test_the_conflict_message_only_offers_takeover_when_supported(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub, control="terminal")
    with pytest.raises(RcError) as caught:
        await hub.send({"id": "r", "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert "take over first" in caught.value.message

    entry.session.agent = "codex"
    with pytest.raises(RcError) as caught:
        await hub.send({"id": "r2", "session_id": "sess-1", "text": "go", "mode": "auto"})
    assert "take over first" not in caught.value.message
    assert "stop it there" in caught.value.message
    registry.close()


# ----------------------------------------------------------------- finding 23


def test_a_record_longer_than_a_chunk_is_skipped_not_re_read(tmp_path: Path) -> None:
    path = tmp_path / "huge.jsonl"
    path.write_bytes(b"x" * (READ_CHUNK + 4096))
    tail = FileTail(path=str(path))
    assert tail.read_new() == []
    assert tail.offset > 0
    assert tail.skipped_records == 1

    with path.open("ab") as handle:
        handle.write(b'\n{"type": "user"}\n')
    rows = tail.read_new()
    assert rows and rows[-1]["type"] == "user"


# ----------------------------------------------------------------- finding 31


async def test_a_non_numeric_paging_argument_is_a_bad_request(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as caught:
        await hub.history({"session_id": "sess-1", "before_seq": "soon"})
    assert caught.value.code == "bad_request"
    registry.close()


# ------------------------------------------------------------- amendments A7/A8


async def test_a_mirrored_terminal_session_is_running_mid_turn_and_readonly_when_idle(
    tmp_path: Path,
) -> None:
    from rc_client.sessions.mirror import MirrorService

    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub, control="terminal")
    entry.runner = None
    mirror = MirrorService(hub)

    states: list[str] = []
    for control, running in (("terminal", True), ("terminal", False), ("none", False)):
        await mirror._set_control(entry, control, running=running)
        states.append(entry.session.state)
    assert states == ["running", "readonly", "idle"]
    registry.close()


async def test_first_seq_pins_a_block_to_where_it_first_appeared(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/tmp")
    registry.upsert_session(session)
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    channel = SessionChannel(registry, session, publish)
    started = await channel.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="running",
        started_at=0,
    )
    await channel.emit("notice", level="info", text="something in between")
    finished = await channel.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="succeeded",
        started_at=0,
    )
    assert started["first_seq"] == started["seq"] == 1
    assert finished["seq"] == 3
    assert finished["first_seq"] == 1

    events, _ = registry.history("s1")
    stored = next(event for event in events if event["kind"] == "tool_call")
    assert stored["first_seq"] == 1
    block = registry.block("s1", stored["block_id"])
    assert block is not None
    assert block["first_seq"] == 1
    await channel.close()
    registry.close()


async def test_first_seq_survives_a_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    registry = Registry(database)
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/tmp")
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        return None

    channel = SessionChannel(registry, session, publish)
    first = await channel.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="running",
        started_at=0,
    )
    await channel.close()
    registry.close()

    reopened = Registry(database)
    restored = SessionChannel(reopened, session, publish)
    later = await restored.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="succeeded",
        started_at=0,
    )
    assert later["first_seq"] == first["first_seq"]
    assert later["seq"] > first["seq"]
    await restored.close()
    reopened.close()


def test_a_delta_event_never_claims_a_first_seq_it_does_not_own() -> None:
    """`first_seq` is only meaningful for block kinds, which deltas are."""
    from rc_client.events import BLOCK_KINDS

    assert "assistant_text" in BLOCK_KINDS
    assert "status" not in BLOCK_KINDS
    assert "notice" not in BLOCK_KINDS


# ------------------------------------------------------------------- watchdog


def test_the_silence_timeout_matches_the_contract() -> None:
    assert SILENCE_TIMEOUT == 60.0


def test_holder_records_expose_what_terminate_re_checks() -> None:
    holder = Holder(pid=1, identity=(1, "start"), session_id="s", cwd="/repo")
    assert holder.identity == (1, "start")


# ----------------------------------------------------------------- finding 32


def test_question_and_option_ids_are_positional_so_duplicates_cannot_collide() -> None:
    from rc_client.agents.claude.questions import answers_by_prompt, normalise_questions

    questions = normalise_questions(
        {
            "questions": [
                {
                    "header": "Same",
                    "question": "Pick one",
                    "options": [{"label": "Yes"}, {"label": "Yes"}],
                },
                {"header": "Same", "question": "Pick another", "options": [{"label": "No"}]},
            ]
        }
    )
    assert [question["id"] for question in questions] == ["q0", "q1"]
    assert [option["id"] for option in questions[0]["options"]] == ["o0", "o1"]

    resolved = answers_by_prompt(questions, {"q0": ["o1"], "q1": "o0"})
    assert resolved == {"Pick one": ["Yes"], "Pick another": "No"}


# ----------------------------------------------------------------- finding 14


def test_a_codex_patch_approval_can_show_the_streamed_diff() -> None:
    from rc_client.agents.codex.translate import CodexTranslator

    translator = CodexTranslator()
    translator.item(
        {
            "type": "fileChange",
            "id": "fc-1",
            "status": "inProgress",
            "changes": [{"path": "a.py", "diff": "@@\n+one\n"}],
        },
        False,
    )
    diff = translator.pending_diff("fc-1")
    assert diff is not None
    assert diff["path"] == "a.py"
    assert diff["additions"] == 1
    assert translator.pending_diff("missing") is None


# ----------------------------------------------------------------- finding 29


def test_the_launchd_plist_escapes_interpolated_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xml.etree import ElementTree

    from rc_client.service import launchd

    monkeypatch.setenv("PATH", "/opt/a&b:/usr/bin")
    rendered = launchd.render("/opt/tools <beta>/rc-client")
    assert "&amp;" in rendered
    assert "&lt;beta&gt;" in rendered
    ElementTree.fromstring(rendered)


# ----------------------------------------------------------------- finding 25


def test_idempotency_records_expire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rc_client import registry as registry_module

    database = Registry(tmp_path / "state.sqlite3")
    monkeypatch.setattr(registry_module, "now_ms", lambda: 0)
    database.remember_request("s1", "old", {"accepted": "sent"})
    assert database.recall_request("s1", "old") == {"accepted": "sent"}

    monkeypatch.setattr(registry_module, "now_ms", lambda: registry_module.REQUEST_TTL_MS + 1)
    database.remember_request("s1", "fresh", {"accepted": "sent"})
    assert database.recall_request("s1", "old") is None
    assert database.recall_request("s1", "fresh") == {"accepted": "sent"}
    database.close()


# ---------------------------------------------------------------- amendment A9


async def test_after_seq_backfills_what_the_gateway_missed(tmp_path: Path) -> None:
    """A9: the gateway asks for everything past its replay-buffer tail."""
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    for index in range(6):
        await entry.channel.emit("notice", level="info", text=f"n{index}")

    page = await hub.history({"session_id": "sess-1", "after_seq": 3})
    assert [event["seq"] for event in page["events"]] == [4, 5, 6]
    assert page["has_more"] is False

    limited = await hub.history({"session_id": "sess-1", "after_seq": 0, "limit": 2})
    assert [event["seq"] for event in limited["events"]] == [1, 2]
    assert limited["has_more"] is True

    caught_up = await hub.history({"session_id": "sess-1", "after_seq": 6})
    assert caught_up == {"events": [], "has_more": False}
    registry.close()


async def test_after_seq_returns_the_latest_version_of_each_block(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await entry.channel.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="running",
        started_at=0,
    )
    await entry.channel.emit(
        "tool_call",
        block_id="t1",
        tool="Bash",
        tool_kind="shell",
        title="ls",
        status="succeeded",
        started_at=0,
    )
    page = await hub.history({"session_id": "sess-1", "after_seq": 0})
    rows = [event for event in page["events"] if event["kind"] == "tool_call"]
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["first_seq"] == 1
    registry.close()


async def test_before_seq_and_after_seq_cannot_be_combined(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as caught:
        await hub.history({"session_id": "sess-1", "before_seq": 5, "after_seq": 1})
    assert caught.value.code == "bad_request"
    registry.close()


# ------------------------------------- a helper process that vanishes mid-scan


async def test_a_helper_that_vanishes_leaves_the_scan_unfinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ProcessLookupError` used to escape and abort the whole scan round."""
    import asyncio

    from rc_client import procscan

    class Vanishing:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            raise ProcessLookupError

    async def spawn(*args: Any, **kwargs: Any) -> Vanishing:
        return Vanishing()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await procscan.scan_processes() == procscan.Scan(procs=[], complete=False)
    assert await procscan.process_cwds([1, 2]) == ({}, False)
    assert await procscan.file_writers("/tmp/rollout.jsonl") == ([], False)


# ------------------------------- a child that exits between timeout and signal


class VanishingChild:
    """A process that never answers and is already gone when it is signalled."""

    returncode: int | None = None

    def __init__(self) -> None:
        self.signals: list[str] = []

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(30)
        return b"", b""

    def kill(self) -> None:
        self.signals.append("kill")
        raise ProcessLookupError

    def terminate(self) -> None:
        self.signals.append("terminate")
        raise ProcessLookupError

    async def wait(self) -> int:
        return 0


async def test_a_child_that_exits_before_the_kill_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Every helper here signals a child it has just timed out on."""
    import asyncio as aio

    from rc_client import git
    from rc_client.agents.claude import runtime as claude_runtime
    from rc_client.agents.codex import runtime as codex_runtime
    from rc_client.agents.codex.rpc import CodexAppServer

    children: list[VanishingChild] = []

    async def spawn(*args: Any, **kwargs: Any) -> VanishingChild:
        child = VanishingChild()
        children.append(child)
        return child

    monkeypatch.setattr(aio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(claude_runtime, "VERSION_TIMEOUT", 0.01)
    monkeypatch.setattr(codex_runtime, "VERSION_TIMEOUT", 0.01)
    assert await git._git(str(tmp_path), "status", timeout=0.01) == (124, "", "git timed out")
    assert await claude_runtime.probe_version("/bin/claude") is None
    assert await codex_runtime.probe_version("/bin/codex") is None
    assert [child.signals for child in children] == [["kill"], ["kill"], ["kill"]]

    # And the app-server child that has already gone when the session is closed.
    parting = VanishingChild()
    server = CodexAppServer("/bin/codex")
    server._process = parting  # type: ignore[assignment]
    await server.close()
    assert parting.signals == ["terminate"]


# ----------------------------------------- naming the stall that drops the link


async def test_the_probe_reports_a_loop_that_woke_up_late(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stalled loop and a bad network look identical from the gateway."""
    import asyncio as aio
    import logging
    import time as clock

    from rc_client.looplag import watch_loop_lag

    caplog.set_level(logging.WARNING, logger="rc_client.looplag")
    probe = aio.create_task(watch_loop_lag(tick=0.01, threshold=0.05))
    await aio.sleep(0.02)
    clock.sleep(0.2)  # exactly what a synchronous commit does to the loop
    await aio.sleep(0.05)
    probe.cancel()
    with contextlib.suppress(aio.CancelledError):
        await probe
    assert [record.message for record in caplog.records] == ["event loop stalled"]


async def test_the_probe_says_nothing_about_a_loop_that_is_keeping_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio as aio
    import logging

    from rc_client.looplag import watch_loop_lag

    caplog.set_level(logging.WARNING, logger="rc_client.looplag")
    probe = aio.create_task(watch_loop_lag(tick=0.01, threshold=0.5))
    await aio.sleep(0.05)
    probe.cancel()
    with contextlib.suppress(aio.CancelledError):
        await probe
    assert caplog.records == []
