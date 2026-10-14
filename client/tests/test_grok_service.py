"""Joining a Grok session a terminal is running, through the machine's leader.

The service drives a real child process speaking real JSON-RPC
(`tests/fake_grok_agent.py` in one of its `leader` modes), so the spawn, the
framing, the replay and the fan-out of approvals are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts as claude_transcripts
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.grok import cursor as grok_cursor
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok.adapter import GrokRunner
from rc_client.agents.grok.service import GrokLeaderService
from rc_client.config import MirrorConfig
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_grok_runner import Peer, Recorder, shim, wait_for

SESSION = "01a09b4f-1b25-7e92-b96b-356d292ff2d0"
CWD = "/Users/me/dev/gateway"
TOOL_CALL = "call-e5a20e6a-35fc-40c3-b31f-8d5114d01af4-0"
# The replay in `fixtures/grok/resume.jsonl` carries these counters.
PROMPT_INDEX = 4
REPLY_INDEX = 35


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "grok"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text("[cli]\nuse_leader = true\n", encoding="utf-8")
    monkeypatch.setattr(grok_runtime, "home", lambda: directory)
    monkeypatch.delenv("GROK_SANDBOX", raising=False)
    # A scan looks at all three agents; the other two belong to the developer.
    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: [])
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])
    return directory


def register(home: Path, session_id: str = SESSION, pid: int | None = None) -> None:
    """Write Grok's own registry the way a running TUI writes it."""
    (home / "active_sessions.json").write_text(
        json.dumps(
            [
                {
                    "session_id": session_id,
                    "pid": os.getpid() if pid is None else pid,
                    "cwd": CWD,
                    "opened_at": 1789312055,
                }
            ]
        ),
        encoding="utf-8",
    )


def unregister(home: Path) -> None:
    (home / "active_sessions.json").write_text("[]", encoding="utf-8")


def write_summary(home: Path, title: str = "Uninstalling the Codex CLI") -> None:
    directory = home / "sessions" / urllib.parse.quote(CWD, safe="") / SESSION
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps({"info": {"id": SESSION, "cwd": CWD}, "generated_title": title}),
        encoding="utf-8",
    )


def leader_peer(
    tmp_path: Path, mode: str = "leader", loaded: list[str] | None = None, **extra: Any
) -> tuple[str, Peer]:
    binary, peer = shim(tmp_path, mode)
    settings: dict[str, Any] = {"loaded": SESSION if loaded is None else loaded, **extra}
    if isinstance(settings["loaded"], str):
        settings["loaded"] = [settings["loaded"]]
    (peer.directory / "leader.json").write_text(json.dumps(settings), encoding="utf-8")
    (peer.directory / "inject").mkdir(exist_ok=True)
    return binary, peer


def inject(peer: Peer, name: str, payload: dict[str, Any]) -> None:
    """Push a message the leader would have sent unasked."""
    (peer.directory / "inject" / name).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def permission_request(session_id: str = SESSION) -> dict[str, Any]:
    """A request whose options include the one that enables always-approve mode."""
    return {
        "jsonrpc": "2.0",
        "id": 7001,
        "method": "session/request_permission",
        "params": {
            "sessionId": session_id,
            "toolCall": {
                "toolCallId": TOOL_CALL,
                "title": "run_terminal_command",
                "kind": "execute",
                "rawInput": {"command": "rm scratch.txt"},
            },
            "options": [
                {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                {
                    "optionId": "enable-always-approve",
                    "name": "Yes, and don't ask again for anything",
                    "kind": "allow_once",
                },
                {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
            ],
        },
    }


def notification(
    session_id: str, update: dict[str, Any], meta: dict[str, Any] | None = None
) -> Any:
    params: dict[str, Any] = {"sessionId": session_id, "update": update}
    if meta:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "method": "_x.ai/session_notification", "params": params}


def connected(service: GrokLeaderService) -> bool:
    """Whether the leader connection is up, read afresh each time it is asked."""
    return bool(service.ready)


def control_of(entry: SessionEntry) -> str:
    """Who owns the session right now, as a plain string a test can compare."""
    return str(entry.session.control)


def build(tmp_path: Path) -> tuple[SessionHub, Recorder, GrokLeaderService]:
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    info = AgentInfo(agent="grok", available=True, path="/unused")
    hub = SessionHub(registry, recorder, "d1", lambda: [info])
    service = GrokLeaderService(hub)
    hub.grok_leader = service
    return hub, recorder, service


# ------------------------------------------------------------------ adoption


async def test_a_registered_leader_session_becomes_shared(home: Path, tmp_path: Path) -> None:
    write_summary(home)
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    await service.tick(binary)

    entry = hub.entries[SESSION]
    assert entry.session.origin == "terminal"
    assert entry.session.control == "shared"
    assert entry.session.cwd == CWD
    assert entry.session.title == "Uninstalling the Codex CLI"
    assert entry.holder_pid == os.getpid()
    assert service.knows(SESSION) is True
    assert isinstance(entry.runner, GrokRunner)
    assert entry.runner.attached is True
    assert peer.params("session/load")["sessionId"] == SESSION
    await service.stop()


async def test_a_terminal_outside_the_leader_stays_with_the_mirror(
    home: Path, tmp_path: Path
) -> None:
    """`use_leader` off or a sandbox profile: the leader never has the session."""
    register(home)
    binary, peer = leader_peer(tmp_path, loaded=[])
    hub, _, service = build(tmp_path)
    await service.tick(binary)

    assert service.knows(SESSION) is False
    assert SESSION not in hub.entries
    assert "session/load" not in peer.methods()
    await service.stop()


async def test_the_leader_is_left_alone_without_the_flag(home: Path, tmp_path: Path) -> None:
    (home / "config.toml").write_text("[cli]\nuse_leader = false\n", encoding="utf-8")
    register(home)
    binary, peer = leader_peer(tmp_path)
    _, _, service = build(tmp_path)
    await service.tick(binary)

    assert connected(service) is False
    assert peer.methods() == []
    await service.stop()


# ------------------------------------------------------------------- control


async def test_a_terminal_that_left_leaves_a_resumable_session(home: Path, tmp_path: Path) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, recorder, service = build(tmp_path)
    await service.tick(binary)
    assert hub.entries[SESSION].session.control == "shared"

    unregister(home)
    await service.tick(binary)
    entry = hub.entries[SESSION]
    assert entry.session.control == "none"
    # The leader still holds it, so the runner stays attached and a send resumes
    # the very same conversation.
    assert isinstance(entry.runner, GrokRunner)
    assert [event["control"] for event in recorder.events("meta") if "control" in event] == ["none"]
    assert "session/close" not in peer.methods()
    await service.stop()


async def test_a_device_turn_keeps_control_remote(home: Path, tmp_path: Path) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path, mode="leader-cancel")
    hub, recorder, service = build(tmp_path)
    await service.tick(binary)
    entry = hub.entries[SESSION]
    unregister(home)

    assert isinstance(entry.runner, GrokRunner)
    await entry.runner.send("keep going")
    await wait_for(lambda: recorder.events("thinking"))
    await service.tick(binary)
    assert control_of(entry) == "remote"

    await entry.runner.interrupt()
    await wait_for(lambda: recorder.events("turn_completed"))
    await service.tick(binary)
    assert control_of(entry) == "none"
    assert "session/close" not in peer.methods()
    await service.stop()


async def test_a_registered_session_never_has_its_session_closed(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    await service.tick(binary)
    entry = hub.entries[SESSION]
    assert entry.runner is not None
    await entry.runner.close()
    await service.stop()
    assert "session/close" not in peer.methods()


# ------------------------------------------------ A39: closing the session


async def test_closing_a_session_cancels_the_turn_and_unloads_it(
    home: Path, tmp_path: Path
) -> None:
    """The one place `session/close` is sent: a session no terminal is in (A39)."""
    register(home)
    binary, peer = leader_peer(tmp_path, mode="leader-cancel")
    hub, recorder, service = build(tmp_path)
    await service.tick(binary)
    entry = hub.entries[SESSION]
    unregister(home)
    assert isinstance(entry.runner, GrokRunner)
    await entry.runner.send("keep going")
    await wait_for(lambda: recorder.events("thinking"))
    await service.tick(binary)
    assert control_of(entry) == "remote"

    result = await hub.archive({"session_id": SESSION, "archived": True})

    assert "session/cancel" in peer.methods()
    assert peer.params("session/close")["sessionId"] == SESSION
    assert hub.entries[SESSION].runner is None
    session = result["session"]
    assert (session["archived"], session["control"], session["state"]) == (True, "none", "stopped")
    await service.stop()


async def test_closing_never_unloads_a_session_a_terminal_is_in(home: Path, tmp_path: Path) -> None:
    """`session/close` unloads for every client of the leader, the TUI included."""
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    await service.tick(binary)
    entry = hub.entries[SESSION]
    assert control_of(entry) == "shared"

    await hub.archive({"session_id": SESSION, "archived": True})

    assert "session/close" not in peer.methods()
    assert hub.entries[SESSION].runner is None
    await service.stop()


# -------------------------------------------------------------------- replay


async def test_the_replay_is_published_only_above_the_mark_the_mirror_left(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, _ = leader_peer(tmp_path)
    hub, recorder, service = build(tmp_path)
    # The mirror already tailed as far as the thinking block.
    grok_cursor.write(hub.registry, SESSION, PROMPT_INDEX)
    await service.tick(binary)
    await wait_for(lambda: recorder.events("assistant_text"))

    assert recorder.events("user_message") == []
    assert recorder.events("assistant_text")[-1]["text"] == "OK"
    assert grok_cursor.read(hub.registry, SESSION) >= REPLY_INDEX
    await service.stop()


async def test_the_whole_conversation_arrives_when_nothing_was_read_before(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, _ = leader_peer(tmp_path)
    _, recorder, service = build(tmp_path)
    await service.tick(binary)
    await wait_for(lambda: recorder.events("user_message"))

    assert recorder.events("user_message")[0]["source"] == "terminal"
    assert recorder.events("user_message")[0]["text"] == "Reply with exactly OK"
    await service.stop()


async def test_the_mirror_stands_down_for_a_session_the_leader_holds(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    write_summary(home)
    directory = home / "sessions" / urllib.parse.quote(CWD, safe="") / SESSION
    (directory / "updates.jsonl").write_text(
        json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {}}) + "\n",
        encoding="utf-8",
    )
    binary, _ = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    mirror = MirrorService(hub, MirrorConfig(), grok_leader=service)

    await service.tick(binary)
    await mirror.scan_once()
    await mirror.tail_once()
    assert service.knows(SESSION) is True
    assert SESSION not in mirror._grok
    # The mark the mirror keeps is the one the leader reads, so nothing doubles.
    assert grok_cursor.read(hub.registry, SESSION) > 0
    await service.stop()


# ------------------------------------------------------------------- prompts


async def test_the_device_s_own_prompt_is_not_echoed_back_as_a_second_bubble(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, _ = leader_peer(tmp_path)
    hub, recorder, service = build(tmp_path)
    grok_cursor.write(hub.registry, SESSION, 999)
    await service.tick(binary)
    entry = hub.entries[SESSION]
    assert isinstance(entry.runner, GrokRunner)

    await entry.runner.send("Reply with exactly OK")
    await wait_for(lambda: recorder.events("turn_completed"))
    sent = [
        event
        for event in recorder.events("user_message")
        if event["text"] == "Reply with exactly OK"
    ]
    assert len(sent) == 1
    assert sent[0]["source"] == "remote"
    await service.stop()


async def test_a_prompt_typed_at_the_terminal_arrives_as_a_terminal_message(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, recorder, service = build(tmp_path)
    grok_cursor.write(hub.registry, SESSION, 999)
    await service.tick(binary)

    inject(
        peer,
        "10-typed.json",
        notification(
            SESSION,
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hello"}},
            {"eventId": f"{SESSION}-1001", "promptId": "p-1"},
        ),
    )
    await wait_for(lambda: recorder.events("user_message"))
    typed = recorder.events("user_message")[0]
    assert typed["text"] == "hello"
    assert typed["source"] == "terminal"
    assert hub.entries[SESSION].session.turn is not None
    await service.stop()


# ----------------------------------------------------------------- approvals


async def test_the_always_approve_option_is_never_offered(home: Path, tmp_path: Path) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, recorder, service = build(tmp_path)
    grok_cursor.write(hub.registry, SESSION, 999)
    await service.tick(binary)

    inject(peer, "10-permission.json", permission_request())
    await wait_for(lambda: recorder.events("approval"))
    pending = recorder.events("approval")[0]
    assert [option["id"] for option in pending["options"]] == ["allow-once", "reject-once"]
    assert pending["title"] == "rm scratch.txt"
    await service.stop()


async def test_an_approval_answered_elsewhere_ends_as_elsewhere(home: Path, tmp_path: Path) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, recorder, service = build(tmp_path)
    grok_cursor.write(hub.registry, SESSION, 999)
    await service.tick(binary)

    inject(peer, "10-permission.json", permission_request())
    await wait_for(lambda: recorder.events("approval"))
    inject(
        peer,
        "20-resolved.json",
        notification(SESSION, {"sessionUpdate": "interaction_resolved", "tool_call_id": TOOL_CALL}),
    )
    await wait_for(lambda: len(recorder.events("approval")) > 1)
    resolved = recorder.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "elsewhere", "by": "terminal"}
    answers = [message for message in peer.requests() if message.get("method") is None]
    assert answers[-1]["result"] == {"outcome": {"outcome": "cancelled"}}
    await service.stop()


# ------------------------------------------------------------------- titles


async def test_the_leader_s_index_names_a_session_with_no_summary(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, _ = leader_peer(tmp_path, titles={SESSION: "Porting the installer"})
    hub, _, service = build(tmp_path)
    await service.tick(binary)

    assert hub.entries[SESSION].session.title == "Porting the installer"
    await service.stop()


async def test_the_leader_s_own_title_reaches_the_apps(home: Path, tmp_path: Path) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    grok_cursor.write(hub.registry, SESSION, 999)
    await service.tick(binary)

    inject(
        peer,
        "10-changed.json",
        {
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "upserted": [{"sessionId": SESSION, "title": "Rewriting the installer"}],
                "removed": [],
            },
        },
    )
    await wait_for(lambda: hub.entries[SESSION].session.title == "Rewriting the installer")
    await service.stop()


# ------------------------------------------------------------------ the link


async def test_a_leader_that_died_is_rejoined_on_the_next_scan(home: Path, tmp_path: Path) -> None:
    register(home)
    binary, peer = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    await service.tick(binary)
    assert connected(service) is True
    loads = len(peer.calls("session/load"))

    client = service._client
    assert client is not None
    agent = client._agent
    assert agent is not None
    child = agent._process
    assert child is not None
    child.kill()
    await asyncio.sleep(0.2)
    assert connected(service) is False

    await service.tick(binary)
    assert connected(service) is True
    assert len(peer.calls("session/load")) > loads
    assert hub.entries[SESSION].session.control == "shared"
    await service.stop()


# ---------------------------------------------------------- device sessions


async def test_a_session_the_device_creates_runs_on_the_same_leader(
    home: Path, tmp_path: Path
) -> None:
    binary, peer = leader_peer(tmp_path, loaded=[], newSessionId=SESSION)
    hub, _, service = build(tmp_path)
    session = Session(
        session_id="temp-1", device_id="d1", agent="grok", cwd=str(tmp_path), origin="remote"
    )
    entry = hub.register_mirrored(session)
    entry.channel.start()
    assert await service.ensure(binary) is True

    runner = service.runner_for(entry, resume=None)
    await runner.start()
    entry.runner = runner
    await service.publish_control(entry)

    assert runner.attached is True
    assert peer.params("session/new")["cwd"] == str(tmp_path)
    assert hub.entries[SESSION].session.control == "remote"
    await runner.close()
    await service.stop()
