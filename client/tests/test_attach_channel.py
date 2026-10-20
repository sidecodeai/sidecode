"""The channel bridge: JSON-RPC surface, framing and the daemon socket."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rc_client.channel import rpc, wire
from rc_client.channel.bridge import ChannelBridge
from rc_client.channel.link import DaemonLink
from rc_client.sessions import attach as attach_module
from rc_client.sessions.attach import (
    Attachment,
    AttachServer,
    HookQuestion,
    SessionStart,
    TurnHook,
)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory shallow enough for a Unix socket path (104 bytes on macOS)."""
    path = Path("/tmp") / f"rc-t-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_frames_round_trip_and_malformed_lines_are_dropped() -> None:
    frame = wire.inject("m1", "héllo\tthere")
    assert frame is wire.decode(wire.encode(frame)) or wire.decode(wire.encode(frame)) == frame
    assert wire.encode(frame).endswith(b"\n")
    assert b"\n" not in wire.encode(frame)[:-1]
    for bad in (b"", b"   \n", b"not json\n", b"[1,2]\n", b'"text"\n'):
        assert wire.decode(bad) is None


def test_initialize_declares_both_channel_capabilities_and_no_tools() -> None:
    reply = rpc.handle_request("initialize", 0, {"protocolVersion": "2025-11-25"})
    assert reply is not None
    result = reply["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"]["experimental"] == {
        "claude/channel": {},
        "claude/channel/permission": {},
    }
    assert result["capabilities"]["tools"] == {}
    assert rpc.handle_request("tools/list", 1, {})["result"] == {"tools": []}  # type: ignore[index]
    assert rpc.handle_request("ping", 2, {})["result"] == {}  # type: ignore[index]
    assert "error" in rpc.handle_request("nope", 3, {})  # type: ignore[operator]
    assert rpc.handle_request("notifications/whatever", None, {}) is None


def test_an_injected_message_carries_our_id_in_its_meta() -> None:
    frame = rpc.channel_message("m7", "hello")
    assert frame["method"] == rpc.CHANNEL_NOTIFICATION
    assert frame["params"]["meta"] == {"origin": "app", "message_id": "m7"}
    assert frame["params"]["content"] == "hello"


class RecordingLink:
    def __init__(self) -> None:
        self.registration: dict[str, Any] | None = None
        self.sent: list[dict[str, Any]] = []
        self.started = False

    def register(self, message: dict[str, Any]) -> None:
        self.registration = message

    def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def start(self) -> None:
        self.started = True


def test_the_bridge_registers_on_initialize_and_relays_permission_requests() -> None:
    link = RecordingLink()
    bridge = ChannelBridge(link, "sess-1", "/repo")  # type: ignore[arg-type]

    reply = bridge.on_rpc(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"clientInfo": {"name": "claude-code", "version": "2.1.267"}},
        }
    )
    assert reply is not None and reply["id"] == 0
    assert link.started
    assert link.registration is not None
    assert link.registration["session_id"] == "sess-1"
    assert link.registration["cwd"] == "/repo"
    assert link.registration["claude_version"] == "2.1.267"

    assert (
        bridge.on_rpc(
            {
                "jsonrpc": "2.0",
                "method": rpc.PERMISSION_REQUEST_NOTIFICATION,
                "params": {
                    "request_id": "abc",
                    "tool_name": "Write",
                    "description": "Write a file",
                    "input_preview": '{"file_path": "/tmp/x"}',
                },
            }
        )
        is None
    )
    assert link.sent == [
        {
            "type": "permission_request",
            "request_id": "abc",
            "tool_name": "Write",
            "description": "Write a file",
            "input_preview": '{"file_path": "/tmp/x"}',
        }
    ]
    # A second initialize must not register twice.
    bridge.on_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert link.registration["claude_version"] == "2.1.267"


class RecordingSink:
    def __init__(self) -> None:
        self.registered: list[Attachment] = []
        self.permissions: list[dict[str, Any]] = []
        self.closed: list[Attachment] = []
        self.starts: list[SessionStart] = []
        self.questions: list[HookQuestion] = []
        self.finished: list[HookQuestion] = []
        self.saw_close = asyncio.Event()
        self.saw_permission = asyncio.Event()
        self.saw_question = asyncio.Event()
        self.saw_question_closed = asyncio.Event()
        # What the daemon would answer the next question hook that arrives.
        self.answer: dict[str, Any] | None = None
        # A51: the turn hooks heard, and what the daemon answers each of them.
        self.turns: list[TurnHook] = []
        self.turn_reply: dict[str, str] = {}

    async def attach_registered(self, attachment: Attachment) -> None:
        self.registered.append(attachment)

    async def attach_permission_request(
        self, attachment: Attachment, payload: dict[str, Any]
    ) -> None:
        self.permissions.append(payload)
        self.saw_permission.set()

    async def attach_closed(self, attachment: Attachment) -> None:
        self.closed.append(attachment)
        self.saw_close.set()

    async def attach_session_started(self, start: SessionStart) -> None:
        self.starts.append(start)

    async def attach_question(self, question: HookQuestion) -> None:
        self.questions.append(question)
        self.saw_question.set()
        if self.answer is not None:
            await question.answer(self.answer)

    async def attach_question_closed(self, question: HookQuestion) -> None:
        self.finished.append(question)
        self.saw_question_closed.set()

    async def attach_turn(self, hook: TurnHook) -> dict[str, str]:
        self.turns.append(hook)
        return dict(self.turn_reply)


async def test_a_bridge_and_the_daemon_talk_over_the_unix_socket(short_dir: Path) -> None:
    sink = RecordingSink()
    server = AttachServer(short_dir / "channel.sock", sink)
    await server.start()
    inbound: list[dict[str, Any]] = []
    received = asyncio.Event()

    async def on_message(message: dict[str, Any]) -> None:
        inbound.append(message)
        received.set()

    link = DaemonLink(short_dir / "channel.sock", on_message)
    link.register(wire.register("sess-9", "/repo", 4242, "2.1.267"))
    link.start()
    try:
        await asyncio.wait_for(_until(lambda: bool(sink.registered)), timeout=2)
        attachment = sink.registered[0]
        assert (attachment.session_id, attachment.cwd, attachment.pid) == ("sess-9", "/repo", 4242)
        assert attachment.claude_version == "2.1.267"

        await asyncio.wait_for(received.wait(), timeout=2)
        assert inbound[0] == {"type": wire.REGISTERED}

        received.clear()
        assert await attachment.inject("m1", "hello")
        await asyncio.wait_for(received.wait(), timeout=2)
        assert inbound[1] == {"type": "inject", "message_id": "m1", "text": "hello"}

        link.send(wire.permission_request({"request_id": "r1", "tool_name": "Bash"}))
        await asyncio.wait_for(sink.saw_permission.wait(), timeout=2)
        assert sink.permissions[0]["request_id"] == "r1"

        link.send(wire.closed())
        await asyncio.wait_for(sink.saw_close.wait(), timeout=2)
        assert sink.closed == [attachment]
        assert attachment.alive is False
        assert await attachment.inject("m2", "late") is False
    finally:
        await link.stop()
        await server.stop()
    assert not (short_dir / "channel.sock").exists()


async def test_a_bridge_started_before_the_daemon_buffers_and_reconnects(
    short_dir: Path,
) -> None:
    socket = short_dir / "channel.sock"
    sink = RecordingSink()

    async def on_message(message: dict[str, Any]) -> None:
        return None

    link = DaemonLink(socket, on_message)
    link.register(wire.register("sess-late", "/repo", 7, None))
    link.send(wire.permission_request({"request_id": "r0", "tool_name": "Read"}))
    link.start()
    await asyncio.sleep(0.3)

    server = AttachServer(socket, sink)
    await server.start()
    try:
        await asyncio.wait_for(sink.saw_permission.wait(), timeout=5)
        assert sink.registered[0].session_id == "sess-late"
        assert sink.permissions[0]["request_id"] == "r0"
    finally:
        await link.stop()
        await server.stop()


async def test_the_session_start_hook_delivers_one_frame_and_hangs_up(short_dir: Path) -> None:
    """The hook is not an attachment: it says one thing, is answered nothing, and goes."""
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    server = AttachServer(socket, sink)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(
            wire.encode(
                wire.session_start("sess-new", "/repo", 4242, "resume", "/transcripts/new.jsonl")
            )
        )
        await writer.drain()
        assert await reader.read() == b"", "the daemon says nothing back to a hook"
        writer.close()
        await asyncio.wait_for(_until(lambda: bool(sink.starts)), timeout=2)
    finally:
        await server.stop()

    assert sink.starts == [
        SessionStart(
            session_id="sess-new",
            cwd="/repo",
            pid=4242,
            source="resume",
            transcript_path="/transcripts/new.jsonl",
        )
    ]
    assert sink.registered == []


async def test_an_unusable_session_start_frame_is_dropped(short_dir: Path) -> None:
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    server = AttachServer(socket, sink)
    await server.start()
    unusable: list[dict[str, Any]] = [
        {"type": wire.SESSION_START, "session_id": "", "pid": 7},
        {"type": wire.SESSION_START, "session_id": "with space", "pid": 7},
        {"type": wire.SESSION_START, "session_id": "sess-new", "pid": 0},
        {"type": wire.SESSION_START, "session_id": "sess-new", "pid": "not a number"},
    ]
    try:
        for frame in unusable:
            reader, writer = await asyncio.open_unix_connection(str(socket))
            writer.write(wire.encode(frame))
            await writer.drain()
            assert await reader.read() == b""
            writer.close()
        # An unknown `source` is not a reason to drop the frame: the session
        # moved either way, and Claude Code may name a source we do not know.
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(wire.encode(dict(wire.session_start("s1", "/repo", 9, "later", ""))))
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
        await asyncio.wait_for(_until(lambda: bool(sink.starts)), timeout=2)
    finally:
        await server.stop()
    assert [start.source for start in sink.starts] == ["startup"]


async def test_a_question_hook_waits_on_the_socket_until_it_is_answered(
    short_dir: Path,
) -> None:
    """Amendment A20: the hook holds its connection open for the answer."""
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    sink.answer = {"Which fix?": "Go monotonic"}
    server = AttachServer(socket, sink)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(
            wire.encode(
                wire.question("sess-q", "/repo", "AskUserQuestion", {"questions": [{"id": "q1"}]})
            )
        )
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        assert wire.decode(line) == {"type": "answers", "answers": sink.answer}
        writer.close()
        await asyncio.wait_for(sink.saw_question_closed.wait(), timeout=2)
    finally:
        await server.stop()

    assert [question.session_id for question in sink.questions] == ["sess-q"]
    assert sink.questions[0].input == {"questions": [{"id": "q1"}]}
    assert sink.finished == sink.questions


async def test_a_question_hook_that_goes_away_first_is_reported_as_closed(
    short_dir: Path,
) -> None:
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    server = AttachServer(socket, sink)
    await server.start()
    try:
        _, writer = await asyncio.open_unix_connection(str(socket))
        writer.write(wire.encode(wire.question("sess-q", "/repo", "AskUserQuestion", {})))
        await writer.drain()
        await asyncio.wait_for(sink.saw_question.wait(), timeout=2)
        writer.close()
        await asyncio.wait_for(sink.saw_question_closed.wait(), timeout=2)
    finally:
        await server.stop()
    assert sink.finished == sink.questions


async def test_an_unusable_question_frame_is_dropped(short_dir: Path) -> None:
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    server = AttachServer(socket, sink)
    await server.start()
    try:
        for frame in (
            {"type": wire.QUESTION, "session_id": "", "input": {}},
            {"type": wire.QUESTION, "session_id": "with space", "input": {}},
            {"type": wire.QUESTION, "session_id": "sess-q", "input": "not an object"},
        ):
            reader, writer = await asyncio.open_unix_connection(str(socket))
            writer.write(wire.encode(frame))
            await writer.drain()
            assert await reader.read() == b""
            writer.close()
    finally:
        await server.stop()
    assert sink.questions == []


async def test_a_turn_hook_gets_one_reply_and_the_connection_ends(short_dir: Path) -> None:
    """A51: a turn hook asks, is answered once, and goes."""
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    sink.turn_reply = {"context": "the words", "system_message": "the line"}
    server = AttachServer(socket, sink)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket))
        frame = {"type": wire.TURN, "event": "batch", "session_id": "sess-1"}
        writer.write(wire.encode(frame))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2)
        assert wire.decode(line) == {
            "type": "turn_reply",
            "context": "the words",
            "system_message": "the line",
        }
        assert await reader.read() == b""
        writer.close()
    finally:
        await server.stop()
    assert sink.turns == [TurnHook("sess-1", "batch")]


def test_a_turn_hooks_deadline_is_kept_only_when_it_is_soon() -> None:
    """A51: the daemon does not begin what it could not answer before the hook gives up."""
    frame = {"type": wire.TURN, "event": "batch", "session_id": "s1"}
    soon = time.time() + 0.8
    hook = attach_module._turn_hook({**frame, "deadline": soon})
    assert hook is not None and hook.deadline == soon
    for odd in (time.time() + 3600, time.time() - 1, "soon", None):
        hook = attach_module._turn_hook({**frame, "deadline": odd})
        assert hook is not None and hook.deadline == 0.0


async def test_a_terminal_says_whether_its_cli_has_the_batch_hook(short_dir: Path) -> None:
    """A51: the proxy picked the settings file, and only it knows which."""
    socket = short_dir / "channel.sock"
    server = AttachServer(socket, RecordingSink())
    await server.start()
    try:
        writers = []
        for pid, tool_batch in ((1234, True), (5678, False)):
            _, writer = await asyncio.open_unix_connection(str(socket))
            writer.write(wire.encode(wire.pty_register(pid, tool_batch)))
            await writer.drain()
            writers.append(writer)
        for _ in range(100):
            if server.ptys.pids() == [1234, 5678]:
                break
            await asyncio.sleep(0.02)
        first, second = server.ptys.get(1234), server.ptys.get(5678)
        assert first is not None and first.tool_batch is True
        assert second is not None and second.tool_batch is False
        for writer in writers:
            writer.close()
    finally:
        await server.stop()


async def test_a_prompt_hook_names_whose_prompt_and_nothing_else(short_dir: Path) -> None:
    socket = short_dir / "channel.sock"
    sink = RecordingSink()
    server = AttachServer(socket, sink)
    await server.start()
    ours = {"prompt_kind": "ours", "message_id": "m-9"}
    frames: list[dict[str, Any]] = [
        {"type": wire.TURN, "event": "prompt", "session_id": "s1", **ours},
        {"type": wire.TURN, "event": "prompt", "session_id": "s1", "prompt_kind": "elsewhere"},
        {"type": wire.TURN, "event": "later", "session_id": "s1"},
        {"type": wire.TURN, "event": "stop", "session_id": "not one"},
    ]
    try:
        for frame in frames:
            reader, writer = await asyncio.open_unix_connection(str(socket))
            writer.write(wire.encode(frame))
            await writer.drain()
            await reader.read()
            writer.close()
    finally:
        await server.stop()
    assert sink.turns == [
        TurnHook("s1", "prompt", "ours", "m-9"),
        TurnHook("s1", "prompt", "", ""),
    ]


async def test_the_bridge_confirms_each_injection_once_it_is_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A51: a held injection is confirmed when it is finally written, never before."""
    link = RecordingLink()
    bridge = ChannelBridge(link, "sess-1", "/repo")  # type: ignore[arg-type]
    await bridge.on_daemon(wire.inject("m1", "early"))
    assert link.sent == []
    bridge.on_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    await bridge.listen()
    await bridge.on_daemon(wire.inject("m2", "later"))
    assert link.sent == [wire.injected("m1"), wire.injected("m2")]
    written = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [frame["params"]["meta"]["message_id"] for frame in written] == ["m1", "m2"]
    assert link.registration is None or link.registration.get("acks") is True


async def test_a_stop_hook_can_wait_until_the_bridge_has_written(short_dir: Path) -> None:
    sink = RecordingSink()
    server = AttachServer(short_dir / "channel.sock", sink)
    await server.start()
    link: DaemonLink

    async def on_message(message: dict[str, Any]) -> None:
        if message.get("type") == wire.INJECT:
            link.send(wire.injected(str(message["message_id"])))

    link = DaemonLink(short_dir / "channel.sock", on_message)
    link.register(wire.register("sess-9", "/repo", 4242, "2.1.303"))
    link.start()
    try:
        await asyncio.wait_for(_until(lambda: bool(sink.registered)), timeout=2)
        attachment = sink.registered[0]
        assert attachment.acks is True
        assert await attachment.inject("m1", "hello")
        assert await attachment.landed(["m1"], 2.0) is True
        # A confirmation that came before anyone asked still counts.
        assert await attachment.inject("m2", "again")
        await asyncio.sleep(0.2)
        assert await attachment.landed(["m2"], 0.01) is True
        assert await attachment.landed(["never-sent"], 0.05) is False
    finally:
        await link.stop()
        await server.stop()


async def test_a_bridge_from_before_confirms_nothing_and_is_not_waited_on() -> None:
    attachment = Attachment(session_id="s", cwd="/", pid=1, claude_version=None)
    started = asyncio.get_running_loop().time()
    assert await attachment.landed(["m1"], 5.0) is False
    assert asyncio.get_running_loop().time() - started < 0.5


async def test_a_connection_that_never_registers_is_dropped(short_dir: Path) -> None:
    sink = RecordingSink()
    server = AttachServer(short_dir / "channel.sock", sink)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(short_dir / "channel.sock"))
        writer.write(wire.encode({"type": "permission_request"}))
        await writer.drain()
        assert await reader.read() == b""
        writer.close()
    finally:
        await server.stop()
    assert sink.registered == []


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.02)
