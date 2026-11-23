"""How the device opens an Antigravity conversation it runs no process for (A64, 4.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import errors, opening, presence
from rc_client.agents.antigravity.presence import Holder, Holders
from rc_client.agents.antigravity.replay import Ledger
from rc_client.agents.antigravity.service import AntigravityService
from rc_client.errors import RcError
from tests.agy_helpers import SESSION, Recorder, entry_for, fake_agy, runner_for, wait_for

UPDATED = "2026-11-22T11:19:33Z"
UPDATED_MS = 1795346373000


def listing(sid: str = SESSION, updated: str = UPDATED, cwd: str = "/work") -> list[dict[str, Any]]:
    return [{"sessionId": sid, "cwd": cwd, "title": "Listed Title", "updatedAt": updated}]


def text(role: str, words: str) -> dict[str, Any]:
    kind = "user_message_chunk" if role == "user" else "agent_message_chunk"
    return {"sessionUpdate": kind, "content": {"type": "text", "text": words}}


def tool(call_id: str, command: str) -> dict[str, Any]:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "name": "run_command",
        "kind": "execute",
        "status": "completed",
        "title": command,
        "rawInput": {"CommandLine": command},
        "rawOutput": {"output": "ok", "exit_code": 0},
    }


def holders(found: Holders) -> Any:
    async def scan(binary: str | None, *, cwds: bool) -> Holders:
        return found

    return scan


async def test_the_device_s_own_last_write_resumes_with_nothing_replayed(tmp_path: Path) -> None:
    binary, peer = fake_agy(tmp_path, list=listing())
    recorder = Recorder()
    entry = entry_for(
        tmp_path,
        recorder,
        session_id=SESSION,
        model="gemini-3.8-flash",
        effort="low",
        permission_mode="plan",
    )
    Ledger(wrote=UPDATED_MS + 1000).save(entry.channel.registry, SESSION)
    service = AntigravityService()
    plan = await opening.plan(service, entry, binary)
    assert plan.kind == "resume"

    runner = runner_for(entry, binary, opening=plan, service=service)
    await runner.start()
    await runner.close()
    await service.lister.close()

    methods = [m for m in peer.methods() if m.startswith("session/") and m != "session/list"]
    assert methods[:1] == ["session/resume"]
    assert "session/load" not in methods
    # A new process starts every conversation on the account's defaults: the session's own again.
    sets = [(call["configId"], call["value"]) for call in peer.calls("session/set_config_option")]
    assert sets == [("model", "gemini-3.8-flash"), ("thought_level", "low"), ("mode", "plan")]
    assert not recorder.events("user_message")


async def test_a_conversation_written_elsewhere_replays_only_what_is_new(tmp_path: Path) -> None:
    replay = [
        text("user", "First."),
        text("agent", "One."),
        tool("call_A", "ls"),
        text("agent", "Listed."),
        text("user", "From the terminal."),
        tool("call_B", "pwd"),
        text("agent", "\n\nThere."),
    ]
    binary, peer = fake_agy(tmp_path, list=listing(), replay={SESSION: replay})
    recorder = Recorder()
    entry = entry_for(tmp_path, recorder, session_id=SESSION)
    published = Ledger(
        tools={"call_A"},
        anchor="call_A",
        counts={"": [1, 1], "call_A": [0, 1]},
        wrote=UPDATED_MS - 60_000,
    )
    published.save(entry.channel.registry, SESSION)
    service = AntigravityService()
    plan = await opening.plan(service, entry, binary)
    assert plan.kind == "load"

    runner = runner_for(entry, binary, opening=plan, service=service)
    await runner.start()
    await runner.close()
    await service.lister.close()

    assert [event["text"] for event in recorder.events("user_message")] == ["From the terminal."]
    assert recorder.events("user_message")[0]["source"] == "terminal"
    assert [event["title"] for event in recorder.events("tool_call")] == ["pwd"]
    assert [event["text"] for event in recorder.events("assistant_text")] == ["There."]
    after = Ledger.load(entry.channel.registry, SESSION)
    assert after.tools == {"call_A", "call_B"} and after.anchor == "call_B"
    assert after.wrote == UPDATED_MS, "the device now has every turn agy listed"
    assert peer.calls("session/load") == [
        {"sessionId": SESSION, "cwd": str(tmp_path), "mcpServers": []}
    ]


async def test_a_conversation_known_only_from_the_list_replays_whole(tmp_path: Path) -> None:
    replay = [
        text("user", "Hello."),
        {
            "sessionUpdate": "user_message_chunk",
            "content": {
                "type": "resource_link",
                "uri": "file:///Users/someone/notes.txt",
                "name": "notes.txt",
                "mimeType": "text/plain",
                "size": 5,
            },
        },
        text("agent", "Hi."),
    ]
    binary, _ = fake_agy(tmp_path, list=listing(), replay={SESSION: replay})
    recorder = Recorder()
    entry = entry_for(tmp_path, recorder, session_id=SESSION)
    service = AntigravityService()
    runner = runner_for(entry, binary, opening=await opening.plan(service, entry, binary))
    await runner.start()
    await runner.close()
    await service.lister.close()

    [message] = recorder.events("user_message")
    assert message["text"] == "Hello."
    assert message["attachments"] == [{"name": "notes.txt", "mime": "text/plain", "size": 5}]
    assert [event["text"] for event in recorder.events("assistant_text")] == ["Hi."]


async def test_a_conversation_agy_no_longer_lists_is_never_loaded(tmp_path: Path) -> None:
    binary, peer = fake_agy(tmp_path, list=[])
    entry = entry_for(tmp_path, Recorder(), session_id=SESSION)
    service = AntigravityService()
    with pytest.raises(RcError) as caught:
        await opening.plan(service, entry, binary)
    await service.lister.close()
    assert (caught.value.code, caught.value.message) == ("not_found", errors.GONE)
    assert "session/load" not in peer.methods()


async def test_nobody_signed_in_refuses_the_open_and_is_remembered(tmp_path: Path) -> None:
    binary, _ = fake_agy(tmp_path, signed_out=True)
    entry = entry_for(tmp_path, Recorder(), session_id=SESSION)
    service = AntigravityService()
    with pytest.raises(RcError) as caught:
        await opening.plan(service, entry, binary)
    await service.lister.close()
    assert caught.value.message == errors.SIGNED_OUT
    assert service.signed_out is True


async def test_a_holder_found_just_before_the_open_refuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, peer = fake_agy(tmp_path, list=listing())
    entry = entry_for(tmp_path, Recorder(), session_id=SESSION)
    monkeypatch.setattr(presence, "scan", holders(Holders([Holder(4242, SESSION)])))
    service = AntigravityService()
    with pytest.raises(RcError) as caught:
        await opening.plan(service, entry, binary)
    await service.lister.close()
    assert (caught.value.code, caught.value.message) == ("conflict", errors.HELD)
    assert "session/load" not in peer.methods() and "session/resume" not in peer.methods()


async def test_a_scan_that_cannot_finish_refuses_rather_than_guesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = fake_agy(tmp_path, list=listing())
    entry = entry_for(tmp_path, Recorder(), session_id=SESSION)
    monkeypatch.setattr(presence, "scan", holders(Holders(complete=False)))
    service = AntigravityService()
    with pytest.raises(RcError) as caught:
        await opening.plan(service, entry, binary)
    await service.lister.close()
    assert (caught.value.code, caught.value.message) == ("timeout", errors.SCAN_FAILED)


async def test_a_session_that_never_took_a_prompt_keeps_its_process(tmp_path: Path) -> None:
    binary, _ = fake_agy(tmp_path)
    recorder = Recorder()
    runner = runner_for(entry_for(tmp_path, recorder), binary)
    await runner.start()
    releasable = [runner.private_process]
    await runner.send("Hello.")
    await wait_for(lambda: recorder.events("turn_completed"))
    releasable.append(runner.private_process)
    assert releasable == [False, True], "until its first prompt agy keeps nothing to open again"
    await runner.close()


async def test_the_list_process_opens_no_conversation_and_runs_where_no_project_is(
    tmp_path: Path,
) -> None:
    binary, peer = fake_agy(tmp_path, list=listing())
    service = AntigravityService()
    listed = await service.fresh_list(binary)
    await service.lister.close()
    assert list(listed) == [SESSION]
    [start] = peer.starts()
    assert start["argv"] == ["acp"]
    assert Path(start["cwd"]).name == "lists"
    assert list(Path(start["cwd"]).iterdir()) == []
    assert [m for m in peer.methods() if m.startswith("session/")] == ["session/list"]
    assert peer.calls("session/list") == [{}]
