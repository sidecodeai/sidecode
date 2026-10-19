"""The pi runner against a fake `pi --mode rpc` peer.

The peer is a real child process speaking real JSONL
(`tests/fixtures/pi/fake_pi.py`), so the spawn, the framing and the reader task
are exercised end to end. Its replies are shaped after a session recorded from
the real pi 0.85.1; `docs/CLIENT.md` § "pi" describes the commands and events
under "Sessions this device starts".
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.pi import install as pi_install
from rc_client.agents.pi import paths as pi_paths
from rc_client.agents.pi.adapter import PiRunner
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.helpers import event_validator
from tests.uploads import shown, upload

SESSION = "0f2e1d3a-7c44-4b91-9d0e-2f6a5b8c1d20"
FAKE = Path(__file__).resolve().parent / "fixtures" / "pi" / "fake_pi.py"


class Recorder:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def finals(self, kind: str) -> list[dict[str, Any]]:
        """The completed blocks of `kind`, without the streaming flushes."""
        return [event for event in self.events(kind) if event.get("done")]

    def states(self) -> list[str]:
        return [event["state"] for event in self.events("status")]


class Peer:
    """The fake agent's scratch directory, and what the runner asked it."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def commands(self) -> list[dict[str, Any]]:
        path = self.directory / "commands.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def names(self) -> list[str]:
        return [str(command.get("type")) for command in self.commands()]

    def of(self, command: str) -> list[dict[str, Any]]:
        return [entry for entry in self.commands() if entry.get("type") == command]


def shim(tmp_path: Path, mode: str) -> tuple[str, Peer]:
    """An executable that answers `<binary> --mode rpc …` like pi."""
    directory = tmp_path / f"peer-{mode}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mode").write_text(mode, encoding="utf-8")
    path = tmp_path / f"pi-{mode}"
    path.write_text(
        f'#!/bin/sh\nRC_FAKE_PI_DIR="{directory}" exec "{sys.executable}" "{FAKE}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path), Peer(directory)


def build(tmp_path: Path, mode: str) -> tuple[PiRunner, Recorder, Peer]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id=SESSION, device_id="d1", agent="pi", cwd=str(tmp_path))
    registry.upsert_session(session)
    recorder = Recorder()
    channel = SessionChannel(registry, session, recorder)
    binary, peer = shim(tmp_path, mode)
    runner = PiRunner(
        channel,
        binary=binary,
        cwd=str(tmp_path),
        session_id=SESSION,
        model="anthropic/claude-sonnet-4-5",
        effort="medium",
        permission_mode="on-request",
    )
    return runner, recorder, peer


async def wait_for(check: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for the runner")


async def test_a_turn_streams_the_agent_s_own_events(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("user_message")[0]["text"] == "Reply with exactly OK"
    assert recorder.events("thinking")[-1]["done"] is True
    assert [event["text"] for event in recorder.finals("assistant_text")] == [
        "OK",
        "Listed the directory.",
    ]
    assert recorder.events("tool_call")[-1]["status"] == "succeeded"
    completed = recorder.events("turn_completed")[0]
    assert completed["stop_reason"] == "completed"
    # The totals are pi's own, asked for at the end of the turn.
    assert completed["usage"]["total_tokens"] == 5414
    assert completed["usage"]["cost_usd"] == 0.0213
    assert completed["usage"]["context_window"] == 1000000
    assert recorder.states()[-1] == "idle"
    assert "get_session_stats" in peer.names()


def test_the_session_id_and_the_settings_ride_on_the_process(tmp_path: Path) -> None:
    """pi takes its model and thinking level as flags; `--session-id` upserts.

    The extension is not installed in a test home, so the device passes its
    bundled copy with `-e`; an installed one at this build is loaded by pi on
    its own and the flag disappears.
    """
    runner, _, _ = build(tmp_path, "turn")
    assert runner._spawn_args() == [
        "--session-id",
        SESSION,
        "--no-approve",
        "--model",
        "anthropic/claude-sonnet-4-5",
        "--thinking",
        "medium",
        "-e",
        str(pi_paths.bundled_extension()),
    ]
    pi_install.install()
    assert "-e" not in runner._spawn_args()


def test_the_child_is_told_where_to_dial_and_what_to_enforce(tmp_path: Path) -> None:
    """The extension inside the child needs both, and gets neither by guessing."""
    runner, _, _ = build(tmp_path, "turn")
    env = runner._child_env()
    assert env[pi_paths.SOCKET_ENV] == str(pi_paths.socket_path())
    assert env[pi_paths.MODE_ENV] == "on-request"


async def test_the_session_reports_what_pi_is_actually_running(tmp_path: Path) -> None:
    runner, recorder, _ = build(tmp_path, "turn")
    await runner.start()
    await runner.close()
    meta = recorder.events("meta")
    assert meta[0]["model"] == "anthropic/claude-sonnet-4-5"
    assert meta[0]["effort"] == "medium"


async def test_a_session_id_pi_did_not_keep_is_rekeyed(tmp_path: Path) -> None:
    runner, _, _ = build(tmp_path, "rename")
    seen: list[str] = []

    async def on_session_id(session_id: str) -> None:
        seen.append(session_id)

    runner._on_session_id = on_session_id
    await runner.start()
    await runner.close()
    assert seen == ["reassigned-by-pi"]


async def test_a_prompt_pi_refuses_does_not_open_a_turn(tmp_path: Path) -> None:
    runner, recorder, _ = build(tmp_path, "refuse")
    await runner.start()
    with pytest.raises(RcError) as caught:
        await runner.send("Reply with exactly OK")
    await runner.close()
    assert caught.value.code == "agent_unavailable"
    assert "API key" in recorder.events("error")[0]["message"]
    assert recorder.events("turn_started") == []


async def test_a_steered_message_appears_where_the_agent_read_it(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "steer")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("assistant_text"))
    assert runner.busy is True
    assert await runner.steer("and list the directory", "steer-block") is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    steered = [event for event in recorder.events("user_message") if "list" in event["text"]]
    assert len(steered) == 1
    # Amendment A14: the bubble lands after the output that preceded it.
    assert steered[0]["first_seq"] > recorder.finals("assistant_text")[0]["seq"]
    sent = peer.of("prompt")[-1]
    assert sent["streamingBehavior"] == "steer"


async def test_stopping_clears_the_queue_and_shows_what_was_never_read(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "stall")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("assistant_text"))
    assert await runner.steer("and list the directory", "steer-block") is True
    # Held: the agent has not taken it off its queue yet.
    assert [event["text"] for event in recorder.events("user_message")] == ["Reply with exactly OK"]
    assert await runner.interrupt() is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert peer.names().index("clear_queue") < peer.names().index("abort")
    assert recorder.events("turn_completed")[0]["stop_reason"] == "interrupted"
    assert recorder.events("user_message")[-1]["text"] == "and list the directory"
    assert recorder.events("notice")[-1]["level"] == "warn"
    assert "interrupting" in [event.get("detail") for event in recorder.events("status")]


async def test_pi_leaving_mid_turn_ends_the_turn(tmp_path: Path) -> None:
    runner, recorder, _ = build(tmp_path, "crash")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    # The half-written block is closed rather than left streaming for ever.
    assert recorder.finals("assistant_text")[-1]["text"] == "OK"
    assert "exited" in recorder.events("error")[-1]["message"]
    assert recorder.events("turn_completed")[0]["stop_reason"] == "error"
    assert runner.busy is False


async def test_an_interrupt_without_a_turn_does_nothing(tmp_path: Path) -> None:
    runner, _, peer = build(tmp_path, "turn")
    await runner.start()
    assert await runner.interrupt() is False
    assert await runner.steer("too late") is False
    await runner.close()
    assert "abort" not in peer.names()


async def test_a_live_model_and_thinking_change_go_through_pi(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    await runner.apply_settings(model="openai/gpt-5", permission_mode=None, effort="high")
    await runner.close()
    assert peer.of("set_model")[-1] == {
        "id": peer.of("set_model")[-1]["id"],
        "type": "set_model",
        "provider": "openai",
        "modelId": "gpt-5",
    }
    assert peer.of("set_thinking_level")[-1]["level"] == "high"
    meta = recorder.events("meta")
    assert any(event.get("model") == "openai/gpt-5" for event in meta)
    assert any(event.get("effort") == "high" for event in meta)


async def test_a_model_pi_does_not_know_is_reported_rather_than_swallowed(
    tmp_path: Path,
) -> None:
    runner, _, _ = build(tmp_path, "turn")
    await runner.start()
    with pytest.raises(RcError) as caught:
        await runner.apply_settings(model="anthropic/nope", permission_mode=None, effort=None)
    await runner.close()
    assert caught.value.code == "bad_request"


async def test_a_speed_tier_is_unsupported_and_a_permission_mode_is_not(tmp_path: Path) -> None:
    """A26: the modes are the device's, so `session.set` applies them."""
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    with pytest.raises(RcError) as speed:
        await runner.apply_settings(None, None, None, speed="priority")
    await runner.apply_settings(None, "untrusted", None)
    await runner.close()
    assert speed.value.code == "unsupported"
    assert runner.permission_mode == "untrusted"
    assert any(event.get("permission_mode") == "untrusted" for event in recorder.events("meta"))
    # A refused setting changes nothing: pi was never asked.
    assert "set_thinking_level" not in peer.names()


async def test_an_image_rides_on_the_prompt_and_nothing_else_does(tmp_path: Path) -> None:
    """A26: pi takes images through `prompt.images`, in its own content shape.

    The uploads are the protocol's own `AttachmentUpload`, as every app sends it.
    """
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    image = upload()
    await runner.send("look", [image])
    with pytest.raises(RcError) as caught:
        await runner.send("read", [upload("notes.pdf", "application/pdf")])
    await runner.close()
    assert [command["images"] for command in peer.of("prompt")] == [
        [{"type": "image", "data": image["data_base64"], "mimeType": "image/png"}]
    ]
    assert caught.value.code == "unsupported"
    # The bubble names the file it carried; the refused message has none.
    assert [event.get("attachments") for event in recorder.events("user_message")] == [
        [shown(image)]
    ]


async def test_an_approval_nobody_raised_cannot_be_answered(tmp_path: Path) -> None:
    """Questions come from the extension; pi itself never asks anything."""
    runner, _, _ = build(tmp_path, "turn")
    await runner.start()
    assert await runner.approve("whatever", "allow", None) is False
    assert await runner.answer("whatever", {}) is False
    await runner.close()


async def test_every_event_a_turn_publishes_validates(tmp_path: Path) -> None:
    validator = event_validator()
    if validator is None:
        return
    runner, recorder, _ = build(tmp_path, "turn")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()
    for frame in recorder.frames:
        if frame.get("type") == "session.event":
            validator.validate(frame["event"])


async def test_the_package_is_the_plugin_the_registry_expects(tmp_path: Path) -> None:
    """`AGENT_IDS` gains `"pi"` at merge; everything else is already in place."""
    from rc_client.agents.pi import plugin
    from rc_client.agents.registry import RunnerSpec
    from rc_client.models import AgentInfo
    from rc_client.sessions.hub import SessionEntry

    assert plugin.AGENT == "pi"
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id=SESSION, device_id="d1", agent="pi", cwd=str(tmp_path))
    channel = SessionChannel(registry, session, Recorder())
    spec = RunnerSpec(
        entry=SessionEntry(session=session, channel=channel),
        info=AgentInfo(agent="pi", available=False),
        resume=None,
        on_turn_end=_nothing,
        on_session_id=_named,
    )
    with pytest.raises(RcError) as caught:
        await plugin.build_runner(spec)
    assert caught.value.code == "agent_unavailable"


async def _nothing() -> None:
    return None


async def _named(session_id: str) -> None:
    return None


def test_the_fake_peer_is_readable() -> None:
    assert FAKE.stat().st_size > 0
    assert stat.S_ISREG(FAKE.stat().st_mode)
