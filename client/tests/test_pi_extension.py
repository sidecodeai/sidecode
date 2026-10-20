"""The pi extension: installing it, and the socket it speaks to the device on.

The extension itself is TypeScript running inside pi, so what is exercised here
is the device's half: the frames it accepts, the session a `hello` creates, the
approvals it raises and answers, and the CLI that puts the file in place. A
fake extension client stands in for the real one, sending the frames that
`docs/CLIENT.md` § "pi" describes under "The socket" and "Approvals".
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.pi import commands as pi_commands
from rc_client.agents.pi import frames, install
from rc_client.agents.pi import paths as pi_paths
from rc_client.agents.pi.link import Hello, PiExtensionServer, PiLink
from rc_client.agents.pi.service import PiExtensionService
from rc_client.agents.pi.terminal import PiTerminalSession
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Choice
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub
from tests.uploads import shown, upload

SESSION = "7f0a2b44-1c3d-4e5f-8a9b-0c1d2e3f4a5b"
# A request id as apps send them, which the bubble keeps (A12).
STEERED = "0d6b8e29-4a17-4c3f-9b52-8e1a6f70d3c4"

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pi"


def real_pi() -> str | None:
    """The pi on this machine, if there is one. Never used for a model turn."""
    return shutil.which("pi") or next(
        (
            str(path)
            for path in (Path("/opt/homebrew/bin/pi"), Path("/usr/local/bin/pi"))
            if path.is_file()
        ),
        None,
    )


def agent_info() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="pi",
            available=True,
            path="/bin/pi",
            models=[Choice("xai/grok-4.6", "Grok 4.6")],
            default_model="xai/grok-4.6",
            permission_modes=[
                Choice("untrusted", "Ask for everything"),
                Choice("on-request", "Ask when needed"),
                Choice("never", "Never ask"),
            ],
            default_permission_mode="on-request",
            efforts=[Choice("off", "Off"), Choice("high", "High")],
            default_effort="off",
            capabilities=["interrupt", "queue", "steer", "attachments", "effort", "history"],
            attach="extension",
            attach_ready=True,
            shared_interrupt=True,
            shared_settings=True,
            shared_attachments=True,
        )
    ]


class Bench:
    """A hub, its published frames, and a live extension socket in front of it."""

    def __init__(self, hub: SessionHub, frames_out: list[dict[str, Any]], path: Path) -> None:
        self.hub = hub
        self.frames = frames_out
        self.path = path
        self.service = PiExtensionService(hub)
        hub.pi_extensions = self.service
        self.server = PiExtensionServer(path, self.service)
        # Closed before the server stops, which waits for every connection: a
        # test that fails before closing its own would otherwise never end.
        self.extensions: list[FakeExtension] = []

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def entry(self, session_id: str = SESSION) -> Any:
        return self.hub.entries[session_id]

    def runner(self, session_id: str = SESSION) -> PiTerminalSession:
        runner = self.entry(session_id).runner
        assert isinstance(runner, PiTerminalSession)
        return runner


class FakeExtension:
    """What the real extension sends, without a pi process behind it."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.seen: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.writer.write(frames.encode(frame))
        await self.writer.drain()

    async def hello(self, **fields: Any) -> dict[str, Any]:
        payload = {
            "type": frames.HELLO,
            "protocol": 1,
            "session_id": SESSION,
            "cwd": "/repo",
            "pid": 4242,
            "mode": frames.TUI,
            "model": "xai/grok-4.6",
            "thinking": "off",
            "name": None,
            "session_file": "/tmp/session.jsonl",
            "entries": [],
        }
        payload.update(fields)
        await self.send(payload)
        return await self.receive()

    async def event(self, event: dict[str, Any]) -> None:
        await self.send({"type": frames.EVENT, "event": event})

    async def receive(self) -> dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), timeout=5.0)
        frame = frames.decode(line)
        assert frame is not None
        self.seen.append(frame)
        return frame

    async def serve_command(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Answer the next command the device sends, the way the extension does."""
        frame = await self.receive()
        await self.send(
            {"type": frames.REPLY, "id": frame["id"], "ok": True, "data": data, "error": None}
        )
        return frame

    def close(self) -> None:
        self.writer.close()


@pytest.fixture
async def bench(tmp_path: Path, socket_dir: Path) -> AsyncIterator[Bench]:
    registry = Registry(tmp_path / "state.sqlite3")
    published: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        published.append(frame)

    hub = SessionHub(registry, publish, "dev-1", agent_info)
    made = Bench(hub, published, socket_dir / "pi-extension.sock")
    await made.server.start()
    try:
        yield made
    finally:
        for extension in made.extensions:
            extension.close()
        await made.server.stop()
        await hub.close()


async def connect(bench: Bench) -> FakeExtension:
    reader, writer = await asyncio.open_unix_connection(str(bench.path))
    extension = FakeExtension(reader, writer)
    bench.extensions.append(extension)
    return extension


async def settle() -> None:
    """Let the server task drain what was just written to it."""
    for _ in range(50):
        await asyncio.sleep(0.002)


# ------------------------------------------------------------------ installing


def test_setup_installs_the_extension_then_says_it_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    first = pi_commands.setup()
    assert first == [f"pi extension installed at {pi_paths.installed_extension()}"]
    assert pi_paths.installed_extension().read_bytes() == pi_paths.bundled_extension().read_bytes()
    # The installer calls this on every run, so repeating it must be quiet.
    assert pi_commands.setup() == [f"pi extension is current at {pi_paths.installed_extension()}"]


def test_setup_without_pi_says_so_and_still_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer treats a non-zero exit as a warning; this is not one."""
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: None)
    assert pi_commands.setup() == [pi_commands.NOT_INSTALLED]
    assert not pi_paths.installed_extension().exists()


def test_remove_takes_the_extension_out_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    pi_commands.setup()
    target = pi_paths.installed_extension()
    assert pi_commands.remove() == [f"removed {target}"]
    assert not target.exists()
    assert pi_commands.remove() == [f"no pi extension at {target}"]


def test_a_stale_copy_is_replaced_rather_than_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    pi_commands.setup()
    pi_paths.installed_extension().write_text("// from an older build\n", encoding="utf-8")
    assert install.ready() is False
    assert pi_commands.setup() == [f"pi extension installed at {pi_paths.installed_extension()}"]
    assert install.ready() is True


def older_copy(*, ours: bool = True) -> Path:
    """A file under the name earlier builds installed the extension as."""
    path = pi_paths.legacy_extension()
    path.parent.mkdir(parents=True, exist_ok=True)
    flag = pi_paths.MARKER.decode() if ours else "__somebodyElse"
    path.write_text(f'// an earlier build\nconst MARKER = "{flag}";\n', encoding="utf-8")
    return path


def test_setup_moves_an_earlier_build_s_copy_onto_the_new_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi finds one copy, at this build, and the old one counts as installed until then."""
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    legacy = older_copy()
    assert install.state() == install.ExtensionState(target=legacy, installed=True, current=False)
    assert pi_commands.setup() == [f"pi extension installed at {pi_paths.installed_extension()}"]
    assert [path.name for path in pi_paths.extensions_dir().iterdir()] == [pi_paths.EXTENSION_NAME]
    assert install.ready() is True


def test_a_copy_left_under_the_old_name_keeps_the_new_one_from_being_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi would load both, and the earlier one might be the copy that acts."""
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    pi_commands.setup()
    legacy = older_copy()
    assert install.ready() is False
    assert pi_commands.setup() == [f"pi extension installed at {pi_paths.installed_extension()}"]
    assert not legacy.exists()
    assert install.ready() is True


def test_a_file_under_the_old_name_that_is_not_ours_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    foreign = older_copy(ours=False)
    before = foreign.read_bytes()
    assert install.state().installed is False
    pi_commands.setup()
    assert install.ready() is True
    assert pi_commands.remove() == [f"removed {pi_paths.installed_extension()}"]
    assert foreign.read_bytes() == before


def test_remove_takes_an_earlier_build_s_copy_out_as_well(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rc_client.agents.pi.runtime.resolve_binary", lambda: "/bin/pi")
    pi_commands.setup()
    legacy = older_copy()
    assert pi_commands.remove() == [
        f"removed {pi_paths.installed_extension()}",
        f"removed {legacy}",
    ]
    assert list(pi_paths.extensions_dir().iterdir()) == []


def test_the_marker_that_tells_our_copies_apart_is_the_extension_s_own() -> None:
    source = pi_paths.bundled_extension().read_bytes()
    assert b'const MARKER = "' + pi_paths.MARKER + b'";' in source


def test_the_extension_is_one_self_contained_file() -> None:
    """pi loads it through jiti with no install step, so it may import nothing."""
    source = pi_paths.bundled_extension().read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith("import ")]
    assert imports, "the extension should import the node built-ins it uses"
    for line in imports:
        assert line.startswith("import type ") or 'from "node:' in line, line


# ---------------------------------------------------------------- the socket


async def test_a_terminal_session_registers_as_shared(bench: Bench) -> None:
    extension = await connect(bench)
    welcome = await extension.hello()
    assert welcome == {"type": frames.WELCOME, "permission_mode": "on-request", "stream": True}
    await settle()
    session = bench.entry().session
    assert session.control == "shared"
    assert session.origin == "terminal"
    assert session.cwd == "/repo"
    assert session.model == "xai/grok-4.6"
    assert session.permission_mode == "on-request"
    extension.close()


async def test_the_branch_a_session_already_had_is_replayed_once(bench: Bench) -> None:
    entries = json.loads((FIXTURES / "branch.json").read_text(encoding="utf-8"))
    extension = await connect(bench)
    await extension.hello(entries=entries)
    await settle()
    assert [event["text"] for event in bench.events("user_message")] == [
        "Run the bash command: echo hello-pi. Then say done."
    ]
    assert [event["text"] for event in bench.events("assistant_text")] == ["done"]
    tool = bench.events("tool_call")[0]
    assert tool["tool"] == "bash"
    assert tool["title"] == "echo hello-pi"
    assert tool["status"] == "succeeded"
    assert tool["output"] == "hello-pi\n"
    # The blocks keep the moment they happened in, not the moment we read them.
    assert bench.events("user_message")[0]["ts"] == 1789323643923
    # The title comes from what the person actually typed.
    assert bench.entry().session.title.startswith("Run the bash command")
    extension.close()

    # A second connection to the same session does not replay it again.
    again = await connect(bench)
    await again.hello(entries=entries)
    await settle()
    assert len(bench.events("user_message")) == 1
    again.close()


async def test_a_prompt_typed_in_the_terminal_opens_a_turn(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await extension.send({"type": frames.INPUT, "source": "interactive", "text": "what changed?"})
    await extension.event({"type": "agent_start"})
    await settle()
    assert bench.events("user_message")[0]["source"] == "terminal"
    assert bench.events("turn_started")[0]["trigger"] == "terminal"
    assert bench.runner().busy is True

    await extension.event(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 0,
                "delta": "two files",
            },
        }
    )
    await extension.event(
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
    )
    await extension.event({"type": "agent_settled"})
    await settle()
    # The turn's totals are asked for over the socket, since there is no RPC.
    stats = await extension.serve_command({"tokens": {"input": 10, "output": 2, "total": 12}})
    assert stats["command"] == frames.STATS
    await settle()
    assert bench.events("turn_completed")[0]["usage"]["total_tokens"] == 12
    assert bench.runner().busy is False
    extension.close()


async def test_a_message_an_app_sends_goes_out_as_a_command(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await settle()
    sending = asyncio.create_task(
        bench.hub.send({"session_id": SESSION, "id": "req-1", "text": "carry on"})
    )
    command = await extension.serve_command()
    assert command["command"] == frames.SEND
    assert command["text"] == "carry on"
    assert command["block_id"] == "req-1"
    assert await sending == {"accepted": "sent"}
    # The bubble appears when pi reports the input, which is where the terminal
    # shows it too, and under the id the app already drew.
    await extension.send(
        {
            "type": frames.INPUT,
            "source": "extension",
            "text": "carry on",
            "block_id": "req-1",
        }
    )
    await settle()
    published = bench.events("user_message")[0]
    assert published["source"] == "remote"
    assert published["text"] == "carry on"
    extension.close()


async def test_a_message_sent_into_a_running_turn_is_steered(bench: Bench) -> None:
    """A14: the bubble waits until pi takes the message off its steering queue."""
    extension = await connect(bench)
    await extension.hello()
    await extension.event({"type": "agent_start"})
    await settle()
    steering = asyncio.create_task(
        bench.hub.send({"session_id": SESSION, "id": "req-3", "text": "and the tests too"})
    )
    command = await extension.serve_command()
    assert command["deliver"] == "steer"
    assert await steering == {"accepted": "steered"}
    await extension.send(
        {
            "type": frames.INPUT,
            "source": "extension",
            "text": "and the tests too",
            "block_id": "req-3",
            "deliver": "steer",
        }
    )
    await extension.event({"type": "queue_update", "steering": ["and the tests too"]})
    await settle()
    assert bench.events("user_message") == []
    # pi read it, so the queue no longer holds it and the bubble belongs here.
    await extension.event({"type": "queue_update", "steering": []})
    await settle()
    assert [event["text"] for event in bench.events("user_message")] == ["and the tests too"]
    extension.close()


async def test_an_image_reaches_the_extension_and_a_pdf_does_not(bench: Bench) -> None:
    """The uploads are the protocol's own `AttachmentUpload`, as every app sends it."""
    extension = await connect(bench)
    await extension.hello()
    await settle()
    image = upload()
    sending = asyncio.create_task(
        bench.hub.send(
            {"session_id": SESSION, "id": "req-2", "text": "what is this?", "attachments": [image]}
        )
    )
    command = await extension.serve_command()
    await sending
    assert command["images"] == [{"data": image["data_base64"], "mime_type": "image/png"}]
    with pytest.raises(RcError) as caught:
        await bench.hub.send(
            {
                "session_id": SESSION,
                "text": "read this",
                "attachments": [upload("notes.pdf", "application/pdf")],
            }
        )
    assert caught.value.code == "unsupported"
    # pi's `input` carries the words alone; the bubble still names the file.
    await extension.send(
        {"type": frames.INPUT, "source": "extension", "text": "what is this?", "block_id": "req-2"}
    )
    await settle()
    assert bench.events("user_message")[-1]["attachments"] == [shown(image)]
    extension.close()


async def test_an_image_sent_into_a_running_turn_is_steered_with_it(bench: Bench) -> None:
    """A14: the bubble waits for pi to read the message, and names its image then."""
    extension = await connect(bench)
    await extension.hello()
    await extension.event({"type": "agent_start"})
    await settle()
    image = upload()
    steering = asyncio.create_task(
        bench.hub.send(
            {"session_id": SESSION, "id": STEERED, "text": "and this one", "attachments": [image]}
        )
    )
    command = await extension.serve_command()
    assert await steering == {"accepted": "steered"}
    assert command["deliver"] == "steer"
    assert command["images"] == [{"data": image["data_base64"], "mime_type": "image/png"}]
    await extension.send(
        {
            "type": frames.INPUT,
            "source": "extension",
            "text": "and this one",
            "block_id": STEERED,
            "deliver": "steer",
        }
    )
    await extension.event({"type": "queue_update", "steering": ["and this one"]})
    await extension.event({"type": "queue_update", "steering": []})
    await settle()
    [bubble] = bench.events("user_message")
    assert (bubble["block_id"], bubble["text"]) == (STEERED, "and this one")
    assert bubble["attachments"] == [shown(image)]
    extension.close()


async def test_a_pdf_sent_into_a_running_turn_is_refused_not_dropped(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await extension.event({"type": "agent_start"})
    await settle()
    sending = bench.hub.send(
        {
            "session_id": SESSION,
            "id": "req-6",
            "text": "and read this",
            "attachments": [upload("notes.pdf", "application/pdf")],
        }
    )
    # Refused at once: a send that reached the extension would wait for it.
    with pytest.raises(RcError) as caught:
        await asyncio.wait_for(sending, timeout=2.0)
    assert caught.value.code == "unsupported"
    # Nothing went to pi: not the words, and not a queued message either.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(extension.reader.readline(), timeout=0.2)
    assert bench.entry().queue == []
    extension.close()


async def test_stop_and_the_settings_all_travel_on_the_link(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await settle()
    changing = asyncio.create_task(
        bench.hub.set_options(
            {
                "session_id": SESSION,
                "model": "xai/grok-4.6",
                "effort": "high",
                "permission_mode": "untrusted",
            }
        )
    )
    sent = [
        (await extension.serve_command())["command"],
        (await extension.serve_command())["command"],
        (await extension.serve_command())["command"],
    ]
    await changing
    assert sent == [frames.SET_MODEL, frames.SET_THINKING, frames.SET_PERMISSION_MODE]
    assert bench.entry().session.permission_mode == "untrusted"
    extension.close()


async def test_the_session_drops_to_none_when_pi_leaves(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await settle()
    await extension.send({"type": frames.BYE, "reason": "quit"})
    extension.close()
    await settle()
    assert bench.entry().session.control == "none"
    assert bench.entry().runner is None


async def test_a_second_pi_in_the_same_terminal_is_a_second_session(bench: Bench) -> None:
    """`/new` shuts one session down and starts another: two rows, not a rename."""
    first = await connect(bench)
    await first.hello()
    await settle()
    second = await connect(bench)
    await second.hello(session_id="another-session")
    await settle()
    assert set(bench.hub.entries) == {SESSION, "another-session"}
    first.close()
    second.close()


# ----------------------------------------------------------------- approvals


async def test_an_approval_is_raised_and_answered_from_an_app(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await extension.event({"type": "agent_start"})
    await extension.send(
        {
            "type": frames.ASK,
            "id": "x1",
            "tool": "bash",
            "input": {"command": "rm -rf build"},
            "cwd": "/repo",
        }
    )
    await settle()
    raised = bench.events("approval")[-1]
    assert raised["status"] == "pending"
    assert raised["tool"] == "bash"
    assert raised["tool_kind"] == "shell"
    assert raised["title"] == "rm -rf build"
    assert [option["id"] for option in raised["options"]] == ["allow", "allow_session", "deny"]
    assert bench.entry().session.state == "needs_approval"

    await bench.hub.approve(
        {"session_id": SESSION, "request_id": raised["request_id"], "option_id": "deny"}
    )
    answer = await extension.receive()
    assert answer == {"type": frames.ANSWER, "id": "x1", "decision": "deny"}
    resolved = bench.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "deny", "by": "remote"}
    extension.close()


async def test_a_question_the_terminal_answered_first_resolves_as_such(bench: Bench) -> None:
    """A20: both sides can answer, and the loser is told what happened."""
    extension = await connect(bench)
    await extension.hello()
    await extension.send(
        {"type": frames.ASK, "id": "x1", "tool": "write", "input": {"path": "a.txt"}}
    )
    await settle()
    raised = bench.events("approval")[-1]
    await extension.send(
        {"type": frames.ASK_CLOSED, "id": "x1", "by": "terminal", "decision": "allow"}
    )
    await settle()
    resolved = bench.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "elsewhere", "by": "terminal"}
    # Answering it now changes nothing: the tool call is long gone.
    assert (await bench.runner().approve(str(raised["request_id"]), "allow", None)) is False
    extension.close()


async def test_an_option_the_block_never_offered_is_refused(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await extension.send({"type": frames.ASK, "id": "x1", "tool": "bash", "input": {}})
    await settle()
    raised = bench.events("approval")[-1]
    with pytest.raises(Exception) as caught:
        await bench.hub.approve(
            {
                "session_id": SESSION,
                "request_id": raised["request_id"],
                "option_id": "allow_always",
            }
        )
    assert getattr(caught.value, "code", "") == "bad_request"
    extension.close()


async def test_an_rpc_extension_with_no_runner_is_turned_away(bench: Bench) -> None:
    """A pi somebody started with `--mode rpc` is not a session of ours."""
    extension = await connect(bench)
    await extension.send(
        {
            "type": frames.HELLO,
            "protocol": 1,
            "session_id": "not-ours",
            "cwd": "/repo",
            "pid": 1,
            "mode": frames.RPC,
            "entries": [],
        }
    )
    assert await extension.reader.read() == b""
    assert "not-ours" not in bench.hub.entries


async def test_a_connection_that_says_nothing_useful_is_dropped(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.send({"type": "who-knows"})
    assert await extension.reader.read() == b""


# ------------------------------------------------------------- the real thing


@pytest.mark.skipif(real_pi() is None, reason="pi is not installed on this machine")
async def test_the_real_pi_loads_the_extension_without_complaint(
    bench: Bench, tmp_path: Path
) -> None:
    """pi compiles the file through jiti, so a syntax or import error is fatal.

    No prompt is sent, nothing is asked of a provider, and the session goes to a
    scratch directory: this asks only whether pi can load the extension,
    register its handlers and reach the socket. pi writes its auth and model
    store into its agent directory as it starts, so it gets a home and an agent
    directory of its own and never sees the person's `~/.pi`.
    """
    binary = real_pi()
    assert binary is not None
    seen: list[Hello] = []

    async def watch(link: PiLink) -> bool:
        seen.append(link.hello)
        return False

    bench.service.link_registered = watch  # type: ignore[method-assign]
    home = tmp_path / "home"
    home.mkdir()
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["PI_CODING_AGENT_DIR"] = str(home / ".pi" / "agent")
    environment[pi_paths.SOCKET_ENV] = str(bench.path)
    environment[pi_paths.MODE_ENV] = "on-request"
    process = await asyncio.create_subprocess_exec(
        binary,
        "--mode",
        "rpc",
        "--session-dir",
        str(tmp_path / "pi-sessions"),
        "--session-id",
        SESSION,
        "-e",
        str(pi_paths.bundled_extension()),
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    printed: list[dict[str, Any]] = []
    try:
        # The extension dials as soon as the session starts, so its hello is
        # the signal that the file loaded and ran.
        for _ in range(300):
            if seen:
                break
            await asyncio.sleep(0.05)
        assert seen, "the real pi never registered through the extension"
        hello = seen[0]
        assert hello.mode == "rpc"
        assert hello.session_id == SESSION
        assert hello.cwd == str(tmp_path)
        assert hello.pid > 0
        assert hello.model is not None
    finally:
        assert process.stdin is not None
        process.stdin.close()
        out, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        printed = [
            json.loads(line) for line in out.decode("utf-8", "replace").splitlines() if line.strip()
        ]
    assert [frame for frame in printed if frame.get("type") == "extension_error"] == []
