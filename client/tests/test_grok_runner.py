"""The Grok runner against a fake `agent agent stdio` peer.

The peer is a real child process speaking real JSON-RPC (`tests/fake_grok_agent.py`),
so the spawn, the framing and the reader task are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from rc_client import attachments
from rc_client.agents.grok import catalog as grok_catalog
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok.adapter import GrokRunner
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionHub
from tests.helpers import event_validator

SESSION = "01a09b4f-1b25-7e92-b96b-356d292ff2d0"
FAKE = Path(__file__).resolve().parent / "fake_grok_agent.py"
SHOT = {"name": "shot.png", "mime": "image/png", "data_base64": "aGVsbG8="}
SHOT_SHOWN = {"name": "shot.png", "mime": "image/png", "size": 5}


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

    def states(self) -> list[str]:
        return [event["state"] for event in self.events("status")]


class Peer:
    """The fake agent's scratch directory, and what the runner asked it."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def requests(self) -> list[dict[str, Any]]:
        path = self.directory / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [
            dict(message.get("params") or {})
            for message in self.requests()
            if message.get("method") == method
        ]

    def methods(self) -> list[str]:
        return [str(message.get("method")) for message in self.requests() if message.get("method")]

    def params(self, method: str) -> dict[str, Any]:
        """The last call of `method`, which is the one a test just made."""
        found: dict[str, Any] = {}
        for message in self.requests():
            if message.get("method") == method:
                found = dict(message.get("params") or {})
        return found


def shim(tmp_path: Path, mode: str) -> tuple[str, Peer]:
    """An executable that answers `<binary> agent --no-leader … stdio` like Grok."""
    directory = tmp_path / f"peer-{mode}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mode").write_text(mode, encoding="utf-8")
    path = tmp_path / f"agent-{mode}"
    path.write_text(
        f'#!/bin/sh\nRC_FAKE_GROK_DIR="{directory}" exec "{sys.executable}" "{FAKE}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path), Peer(directory)


def build(
    tmp_path: Path, mode: str, resume: str | None = None
) -> tuple[GrokRunner, Recorder, Peer]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="temp-1", device_id="d1", agent="grok", cwd=str(tmp_path))
    registry.upsert_session(session)
    recorder = Recorder()
    channel = SessionChannel(registry, session, recorder)
    binary, peer = shim(tmp_path, mode)
    runner = GrokRunner(
        channel,
        binary=binary,
        cwd=str(tmp_path),
        catalog=grok_catalog.load(),
        model="grok-4.6",
        permission_mode="default",
        effort="high",
        resume=resume,
    )
    return runner, recorder, peer


async def wait_for(check: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for the runner")


@pytest.fixture(autouse=True)
def _grok_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grok_runtime, "home", lambda: tmp_path / "grok")


async def test_a_turn_streams_the_agent_s_own_updates(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "turn")
    rekeyed: list[str] = []
    runner._on_session_id = _collect(rekeyed)
    await runner.start()
    assert rekeyed == [SESSION]
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("user_message")[0]["text"] == "Reply with exactly OK"
    assert recorder.events("thinking")[-1]["done"] is True
    assert recorder.events("assistant_text")[-1]["text"] == "OK"
    completed = recorder.events("turn_completed")[0]
    assert completed["stop_reason"] == "completed"
    assert completed["usage"]["total_tokens"] == 25382
    assert recorder.states()[-1] == "idle"
    # The model and the effort ride on the process, the session id on the wire.
    assert peer.params("session/new")["cwd"] == str(tmp_path)
    assert peer.params("session/set_mode")["modeId"] == "default"


def test_the_model_and_effort_are_process_options(tmp_path: Path) -> None:
    """`agent agent` takes no `--cwd` and no `--permission-mode`; those are per session."""
    runner, _, _ = build(tmp_path, "turn")
    assert runner._spawn_args() == ["--model", "grok-4.6", "--reasoning-effort", "high"]
    runner._effort = None
    assert runner._spawn_args() == ["--model", "grok-4.6"]


async def test_an_approval_offers_the_options_the_agent_sent(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "approval")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("approval"))
    pending = recorder.events("approval")[0]
    assert [option["id"] for option in pending["options"]] == [
        "allow-once",
        "allow-always",
        "reject-once",
    ]
    assert [option["style"] for option in pending["options"]] == [
        "primary",
        "secondary",
        "danger",
    ]
    assert pending["title"] == "ls -la"
    assert "needs_approval" in recorder.states()

    assert await runner.approve(pending["request_id"], "allow-once", None) is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()
    resolved = recorder.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "allow-once", "by": "remote"}
    answer = [message for message in peer.requests() if message.get("method") is None]
    assert answer[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow-once"}}


async def test_an_option_the_agent_never_offered_is_not_consent(tmp_path: Path) -> None:
    runner, recorder, _ = build(tmp_path, "approval")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("approval"))
    pending = recorder.events("approval")[0]
    await runner.approve(pending["request_id"], "definitely-yes", None)
    await wait_for(lambda: len(recorder.events("approval")) > 1)
    await runner.close()
    assert recorder.events("approval")[-1]["status"] == "expired"


async def test_an_interrupt_cancels_the_turn(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "cancel")
    await runner.start()
    await runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("thinking"))
    assert await runner.interrupt() is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()
    assert "session/cancel" in peer.methods()
    assert recorder.events("turn_completed")[0]["stop_reason"] == "interrupted"
    # The half-written block is closed rather than left streaming.
    assert recorder.events("thinking")[-1]["done"] is True


async def test_a_message_queued_during_a_turn_goes_out_once_that_turn_is_over(
    tmp_path: Path,
) -> None:
    """A43: the prompt's answer comes after its `turn_completed`, and only then
    is the runner idle, which is when the hub's queue hands over."""
    binary, peer = shim(tmp_path, "turn")
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    info = AgentInfo(agent="grok", available=True, path=binary, capabilities=["queue"])
    hub = SessionHub(registry, recorder, "d1", lambda: [info])
    created = await hub.create({"agent": "grok", "cwd": str(tmp_path)})
    session_id = created["session"]["session_id"]

    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    first = await hub.send({"id": one, "session_id": session_id, "text": "one"})
    second = await hub.send({"id": two, "session_id": session_id, "text": "two"})
    assert (first["accepted"], second["accepted"]) == ("sent", "queued")

    await wait_for(lambda: len(recorder.events("turn_completed")) == 2, timeout=5)
    await asyncio.sleep(0.2)
    assert [call["prompt"][0]["text"] for call in peer.calls("session/prompt")] == ["one", "two"]
    bubbles = [(event["block_id"], event["text"]) for event in recorder.events("user_message")]
    assert bubbles == [(one, "one"), (two, "two")]
    assert len(recorder.events("turn_completed")) == 2
    assert hub.entry(session_id).queue == []
    await hub.close()
    registry.close()


async def test_an_interrupt_without_a_turn_does_nothing(tmp_path: Path) -> None:
    runner, _, peer = build(tmp_path, "turn")
    await runner.start()
    assert await runner.interrupt() is False
    await runner.close()
    assert "session/cancel" not in peer.methods()


async def test_a_live_model_change_goes_through_the_agent(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    await runner.apply_settings(model="grok-4.5", permission_mode="plan", effort="low")
    await runner.close()
    options = {call["configId"]: call["value"] for call in peer.calls("session/set_config_option")}
    assert options == {"model": "grok-4.5", "reasoning_effort": "low"}
    assert peer.params("session/set_mode")["modeId"] == "plan"
    meta = recorder.events("meta")
    assert any(event.get("model") == "grok-4.5" for event in meta)
    assert any(event.get("permission_mode") == "plan" for event in meta)


async def test_a_speed_tier_is_refused(tmp_path: Path) -> None:
    from rc_client.errors import RcError

    runner, _, _ = build(tmp_path, "turn")
    await runner.start()
    with pytest.raises(RcError) as caught:
        await runner.apply_settings(None, None, None, speed="priority")
    await runner.close()
    assert caught.value.code == "unsupported"


async def test_files_reach_grok_as_paths_and_the_apps_as_names(tmp_path: Path) -> None:
    """A49: a prompt carries no image, so it names files Grok opens with `read_file`."""
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    await runner.send("look", [SHOT])
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    [folder] = (attachments.state_root() / "temp-1").iterdir()
    assert peer.params("session/prompt")["prompt"] == [
        {"type": "text", "text": f"look\n\nAttached files:\n- shot.png: {folder / 'shot.png'}"}
    ]
    [bubble] = recorder.events("user_message")
    assert (bubble["text"], bubble["attachments"]) == ("look", [SHOT_SHOWN])
    assert str(attachments.state_root()) not in json.dumps(recorder.frames)


async def test_a_prompt_grok_refuses_takes_its_files_with_it(tmp_path: Path) -> None:
    runner, recorder, _ = build(tmp_path, "refuse")
    await runner.start()
    await runner.send("look", [SHOT])
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert list((attachments.state_root() / "temp-1").iterdir()) == []
    assert recorder.events("turn_completed")[-1]["stop_reason"] == "error"
    assert recorder.events("error")[-1]["message"] == "Invalid prompt"


async def test_a_prompt_grok_failed_on_without_refusing_it_keeps_its_files(tmp_path: Path) -> None:
    """Only a refused request proves the prompt was never read; anything else waits."""
    runner, recorder, _ = build(tmp_path, "break")
    await runner.start()
    await runner.send("look", [SHOT])
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    [folder] = (attachments.state_root() / "temp-1").iterdir()
    assert (folder / "shot.png").exists()
    assert recorder.events("error")[-1]["message"] == "Internal error"


async def test_a_prompt_that_fails_midway_keeps_its_files(tmp_path: Path) -> None:
    """Grok may have opened them already; the age limit deletes them, not the failure."""
    runner, recorder, _ = build(tmp_path, "fail-midway")
    await runner.start()
    await runner.send("look", [SHOT])
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    [folder] = (attachments.state_root() / "temp-1").iterdir()
    assert (folder / "shot.png").exists()
    assert recorder.events("error")[-1]["message"] == "Model overloaded"


async def test_a_send_that_fails_before_the_prompt_goes_out_leaves_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _, peer = build(tmp_path, "turn")
    await runner.start()

    async def broken(trigger: str) -> None:
        raise RuntimeError("the turn could not be announced")

    monkeypatch.setattr(runner.channel, "begin_turn", broken)
    with pytest.raises(RuntimeError):
        await runner.send("look", [SHOT])
    await runner.close()

    assert list((attachments.state_root() / "temp-1").iterdir()) == []
    assert peer.calls("session/prompt") == []


def read_request(path: str) -> dict[str, Any]:
    """Grok 1.0.52 asking to `read_file`, exactly as the leader asked live, at `path`."""
    return {
        "sessionId": "01a13b0d-e7f8-7a11-9a6a-9b31358b7cbe",
        "toolCall": {
            "toolCallId": "call-c24ad0a0-17d2-4088-ab69-a43951064e3e-0",
            "kind": "read",
            "title": f"Read `{path}`",
            "rawInput": {"variant": "ReadFile", "target_file": path},
            "_meta": {
                "x.ai/tool": {
                    "version": 1,
                    "name": "read_file",
                    "kind": "read",
                    "namespace": "grok_build",
                    "label": "Read",
                    "read_only": True,
                    "input": {"path": path},
                },
                "x.ai/requestingSessionId": "01a13b0d-e7f8-7a11-9a6a-9b31358b7cbe",
            },
        },
        "options": [
            {"optionId": "always-allow", "name": "always allow", "kind": "allow_always"},
            {"optionId": "allow-once", "name": "allow once", "kind": "allow_once"},
            {"optionId": "reject-once", "name": "reject once", "kind": "reject_once"},
        ],
    }


async def decision(runner: GrokRunner, recorder: Recorder, request: dict[str, Any]) -> str:
    """`allowed` when the device answered by itself, `asked` when it raised an approval."""
    raised = len(recorder.events("approval"))
    task = asyncio.create_task(runner._handle_permission(request))
    await wait_for(lambda: task.done() or len(recorder.events("approval")) > raised)
    if task.done():
        assert task.result() == {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
        return "allowed"
    await runner.approve(recorder.events("approval")[-1]["request_id"], "reject-once", None)
    await task
    return "asked"


async def test_grok_reads_the_session_s_own_file_without_asking(tmp_path: Path) -> None:
    runner, recorder, _ = build(tmp_path, "turn")
    [own] = attachments.materialise("temp-1", [SHOT])
    assert await decision(runner, recorder, read_request(own.path)) == "allowed"
    assert recorder.events("approval") == []

    unnamed = read_request(own.path)
    del unnamed["toolCall"]["_meta"]
    assert await decision(runner, recorder, unnamed) == "allowed", "its variant is enough"
    always_first = read_request(own.path)
    always_first["options"].insert(
        0, {"optionId": "enable-always-approve", "name": "Yes, always", "kind": "allow_once"}
    )
    assert await decision(runner, recorder, always_first) == "allowed", "and only allow-once"


def variants(own: str) -> dict[str, dict[str, Any]]:
    """Requests that look like reading the session's file and are not only that."""
    cases: dict[str, dict[str, Any]] = {}
    decoy = read_request(own)
    decoy["toolCall"]["rawInput"]["path"] = "/etc/hosts"
    cases["a second path elsewhere"] = decoy
    repeated = read_request(own)
    repeated["toolCall"]["_meta"]["x.ai/tool"]["input"]["path"] = "/etc/hosts"
    cases["the tool's own input elsewhere"] = repeated
    unknown = read_request(own)
    unknown["toolCall"]["rawInput"]["pattern"] = "*"
    cases["a key this device does not know"] = unknown
    widened = read_request(own)
    widened["toolCall"]["_meta"]["x.ai/tool"]["input"]["recursive"] = True
    cases["a key the tool's own input adds"] = widened
    located = read_request(own)
    located["toolCall"]["locations"] = [{"path": own}, {"path": "/etc/hosts"}]
    cases["a location elsewhere"] = located
    listing = read_request(own)
    listing["toolCall"]["_meta"]["x.ai/tool"]["name"] = "list_dir"
    cases["another reading tool"] = listing
    unnamed = read_request(own)
    del unnamed["toolCall"]["_meta"]
    del unnamed["toolCall"]["rawInput"]["variant"]
    cases["a read nothing names as read_file"] = unnamed
    foreign = read_request(own)
    foreign["toolCall"]["_meta"]["x.ai/tool"]["namespace"] = "files"
    cases["an MCP tool of the same name"] = foreign
    executed = read_request(own)
    executed["toolCall"]["kind"] = "execute"
    cases["a call that is not a read"] = executed
    always = read_request(own)
    always["options"] = [
        option for option in always["options"] if option["optionId"] != "allow-once"
    ]
    cases["no option for this call alone"] = always
    return cases


async def test_grok_asks_about_anything_short_of_reading_the_session_s_own_file(
    tmp_path: Path,
) -> None:
    runner, recorder, _ = build(tmp_path, "turn")
    [own] = attachments.materialise("temp-1", [SHOT])
    [other] = attachments.materialise("temp-2", [SHOT])
    cases = {**variants(own.path), "another session's file": read_request(other.path)}
    for name, request in cases.items():
        assert await decision(runner, recorder, request) == "asked", name


async def test_resuming_replays_nothing_into_the_timeline(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "resume", resume=SESSION)
    await runner.start()
    await asyncio.sleep(0.2)
    await runner.close()
    assert peer.params("session/load")["sessionId"] == SESSION
    assert recorder.events("user_message") == []
    assert recorder.events("assistant_text") == []


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


def _collect(sink: list[str]) -> Any:
    async def on_session_id(session_id: str) -> None:
        sink.append(session_id)

    return on_session_id


def test_the_fake_peer_is_executable() -> None:
    assert os.access(FAKE, os.R_OK)
    assert FAKE.stat().st_size > 0
    assert stat.S_ISREG(FAKE.stat().st_mode)
