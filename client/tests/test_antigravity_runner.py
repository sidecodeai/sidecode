"""The Antigravity runner against a fake `agy acp` (A64).

The fake is a real child process speaking real JSON-RPC (`tests/fake_agy.py`), shaped after the
frames the real `agy` wrote, so the spawn, the framing and the ordered handling are exercised end
to end.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from rc_client import __version__
from rc_client.agents.antigravity import runner as runner_module
from rc_client.agents.antigravity.errors import SIGNED_OUT
from rc_client.errors import RcError
from tests.agy_helpers import SESSION, Recorder, entry_for, fake_agy, runner_for, wait_for


def releasable(runner: runner_module.AntigravityRunner) -> bool:
    """Whether the hub may stop the process when unused; read afresh each time it is asked."""
    return runner.private_process


@pytest.fixture(autouse=True)
def quick_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "WAKE_QUIET", 0.2)


async def started(
    tmp_path: Path, **config: Any
) -> tuple[runner_module.AntigravityRunner, Recorder, Any, list[str]]:
    binary, peer = fake_agy(tmp_path, **config)
    recorder = Recorder()
    entry = entry_for(tmp_path, recorder)
    ends: list[str] = []
    runner = runner_for(entry, binary, turn_ends=ends)
    await runner.start()
    return runner, recorder, peer, ends


async def test_a_new_session_says_who_the_device_is_and_nothing_more(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path)
    await runner.close()

    [start] = peer.starts()
    assert start["argv"] == ["acp"], "no flag of any kind beside the subcommand"
    assert Path(start["cwd"]).resolve() == tmp_path.resolve()
    [initialize] = peer.calls("initialize")
    assert initialize == {
        "protocolVersion": 1,
        "clientInfo": {"name": "sidecode", "title": "SideCode", "version": __version__},
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
            "elicitation": {"form": {}},
        },
    }
    assert peer.calls("session/new") == [{"cwd": str(tmp_path), "mcpServers": []}]
    assert "authenticate" not in peer.methods()
    assert runner.session_id == SESSION
    meta = recorder.events("meta")[-1]
    assert (meta["model"], meta["effort"], meta["permission_mode"]) == (
        "gemini-4-neon",
        "high",
        "default",
    )


async def test_a_turn_streams_and_the_tokens_of_each_prompt_add_up(tmp_path: Path) -> None:
    runner, recorder, peer, ends = await started(tmp_path, turns=["ok", "ok"])
    await runner.send("Reply with only the word OK.", block_id="m-1")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.send("Again.", block_id="m-2")
    await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
    await runner.close()

    bubble = recorder.events("user_message")[0]
    assert (bubble["text"], bubble["source"]) == ("Reply with only the word OK.", "remote")
    assert recorder.events("assistant_text")[-1] == {
        **recorder.events("assistant_text")[-1],
        "text": "OK",
        "done": True,
    }
    first, second = recorder.events("turn_completed")
    assert first["stop_reason"] == "completed"
    assert first["usage"]["total_tokens"] == 102
    assert second["usage"]["total_tokens"] == 204, "the session's tokens, summed"
    assert second["usage"]["context_used"] == 9523
    assert second["usage"]["context_window"] == 1048576
    assert "cost_usd" not in second["usage"]
    assert recorder.events("meta")[-1].get("title") or runner.channel.session.title
    assert runner.channel.session.title == "Acknowledge Request"
    prompt = peer.calls("session/prompt")[0]
    assert prompt == {
        "sessionId": SESSION,
        "prompt": [{"type": "text", "text": "Reply with only the word OK."}],
    }, "the person's words, unchanged, and nothing beside them"
    assert ends == ["ended", "ended"]


async def test_what_the_session_asks_for_is_set_in_agy_and_the_level_is_clamped(
    tmp_path: Path,
) -> None:
    binary, peer = fake_agy(tmp_path)
    recorder = Recorder()
    entry = entry_for(
        tmp_path, recorder, model="gemini-3.8-flash", effort="xhigh", permission_mode="plan"
    )
    runner = runner_for(entry, binary)
    await runner.start()
    await runner.close()

    sets = [(call["configId"], call["value"]) for call in peer.calls("session/set_config_option")]
    # Flash offers no `xhigh`: the nearest level below it is set.
    assert sets == [("model", "gemini-3.8-flash"), ("thought_level", "high"), ("mode", "plan")]
    assert (entry.session.model, entry.session.effort, entry.session.permission_mode) == (
        "gemini-3.8-flash",
        "high",
        "plan",
    )


async def test_a_model_without_thinking_levels_runs_with_no_effort(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path)
    await runner.apply_settings("gpt-oss-120b-medium", None, "high")
    await wait_for(lambda: recorder.events("meta")[-1].get("effort", "x") is None)
    await runner.close()

    sets = [(call["configId"], call["value"]) for call in peer.calls("session/set_config_option")]
    assert sets == [("model", "gpt-oss-120b-medium")], "no level is asked of a model with none"
    assert runner.channel.session.effort is None


async def test_an_unknown_model_is_refused_in_agy_s_words(tmp_path: Path) -> None:
    runner, _, _, _ = await started(tmp_path)
    with pytest.raises(RcError) as caught:
        await runner.apply_settings("no-such-model", None, None)
    await runner.close()
    assert caught.value.code == "bad_request"
    assert "Unknown model ID" in caught.value.message


async def test_an_approval_offers_allow_once_and_reject_once_only(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["tool"])
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("approval"))
    pending = recorder.events("approval")[0]
    assert pending["options"] == [
        {"id": "allow", "label": "Allow", "style": "primary"},
        {"id": "deny", "label": "Deny", "style": "danger"},
    ]
    assert (pending["tool"], pending["tool_kind"], pending["title"]) == (
        "run_command",
        "shell",
        "ls -la",
    )
    assert "needs_approval" in recorder.states()

    assert await runner.approve(pending["request_id"], "allow", "a note nobody reads") is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    resolved = recorder.events("approval")[-1]
    assert (resolved["status"], resolved["decision"]) == (
        "resolved",
        {"option_id": "allow", "by": "remote"},
    )
    [reply] = peer.replies()
    assert reply["id"] == "1", "agy's string id comes back as it came"
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "allow"}}
    tool = [e for e in recorder.events("tool_call") if e["status"] == "succeeded"][-1]
    assert tool["summary"] == "exit 0"
    assert recorder.events("turn_completed")[0]["stop_reason"] == "completed"


async def test_a_refusal_ends_the_turn_and_the_tool_fails(tmp_path: Path) -> None:
    runner, recorder, _, _ = await started(tmp_path, turns=["tool"])
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("approval"))
    await runner.approve(recorder.events("approval")[0]["request_id"], "deny", None)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("tool_call")[-1]["status"] == "failed"
    assert recorder.events("turn_completed")[0]["stop_reason"] == "completed"
    assert not [e for e in recorder.events("assistant_text") if e.get("text")]


async def test_an_option_never_offered_is_not_consent(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["tool"])
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("approval"))
    card = recorder.events("approval")[0]
    assert await runner.approve(card["request_id"], "allow_always", None) is True
    await wait_for(lambda: peer.replies())
    await runner.interrupt()
    await runner.close()
    assert peer.replies()[0]["result"] == {"outcome": {"outcome": "cancelled"}}
    assert recorder.events("approval")[-1]["status"] == "expired"


async def test_a_form_is_one_question_block_and_answers_go_back_by_field(
    tmp_path: Path,
) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["question"])
    await runner.send("Ask me.")
    await wait_for(lambda: recorder.events("question"))
    [pending] = recorder.events("question")
    assert pending["questions"] == [
        {
            "id": "q1",
            "prompt": "Which color do you prefer?",
            "options": [{"id": "1", "label": "(Recommended) red"}, {"id": "2", "label": "green"}],
            "multi": False,
            "allow_text": True,
        },
        {
            "id": "q2",
            "prompt": "Which fruits do you like?",
            "options": [
                {"id": "1", "label": "apple"},
                {"id": "2", "label": "banana"},
                {"id": "3", "label": "cherry"},
            ],
            "multi": True,
            "allow_text": True,
        },
    ]
    assert "needs_input" in recorder.states()
    answers = {"q1": ["2"], "q2": "kiwi"}
    assert await runner.answer(pending["request_id"], answers) is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    [reply] = peer.replies()
    assert reply["result"] == {"action": "accept", "content": {"q1": "2", "q2_other": "kiwi"}}
    resolved = recorder.events("question")[-1]
    assert (resolved["status"], resolved["answers"], resolved["by"]) == (
        "resolved",
        answers,
        "remote",
    )


async def test_cards_expire_when_the_turn_is_stopped(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["question"])
    await runner.send("Ask me.")
    await wait_for(lambda: recorder.events("question"))
    assert await runner.interrupt() is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("question")[-1]["status"] == "expired"
    assert recorder.events("turn_completed")[0]["stop_reason"] == "interrupted"
    assert peer.calls("session/cancel") == [{"sessionId": SESSION}]
    assert {"action": "decline"} not in [reply.get("result") for reply in peer.replies()]


async def test_a_message_steers_the_turn_and_its_bubble_comes_when_agy_takes_it(
    tmp_path: Path,
) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["steer"])
    await runner.send("Run sleep 8.", block_id="m-1")
    await wait_for(lambda: recorder.events("tool_call"))
    assert runner.busy and runner.supports_steer
    assert await runner.steer("Also say STEERED.", block_id="m-2") is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    bubbles = [(event["block_id"], event["text"]) for event in recorder.events("user_message")]
    assert [text for _, text in bubbles] == ["Run sleep 8.", "Also say STEERED."]
    [completed] = recorder.events("turn_completed")
    assert completed["usage"]["total_tokens"] == 11550 + 11722, "both prompts' tokens"
    assert len(recorder.events("turn_started")) == 1, "one turn, carried on by the steer"
    assert len(peer.calls("session/prompt")) == 2


async def test_a_message_waits_while_a_card_of_the_turn_is_open(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["tool"])
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("approval"))
    assert await runner.steer("Change of plan.") is False, "it goes to Up next instead"
    assert len(peer.calls("session/prompt")) == 1
    await runner.approve(recorder.events("approval")[0]["request_id"], "allow", None)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()


async def test_a_turn_agy_begins_by_itself_is_the_agent_s(tmp_path: Path) -> None:
    runner, recorder, _, ends = await started(tmp_path, turns=["wake"], wake_delay=0.3)
    await runner.send("Start it in the background.")
    await wait_for(lambda: recorder.events("turn_completed"))
    assert not runner.busy, "the turn is over; the background command is still to come"
    assert releasable(runner) is False
    await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
    await runner.close()

    triggers = [event["trigger"] for event in recorder.events("turn_started")]
    assert triggers == ["remote", "agent"]
    texts = [event["text"] for event in recorder.events("assistant_text") if event.get("done")]
    assert texts == ["STARTED", "FINISHED"], "the blank line agy starts with is trimmed"
    first, second = recorder.events("turn_completed")
    assert second["usage"]["total_tokens"] == first["usage"]["total_tokens"], "no tokens of its own"
    assert second["usage"]["context_used"] == 13213
    assert ends == ["ended", "ended"]
    assert releasable(runner) is True


async def test_a_scheduled_wake_keeps_the_process(tmp_path: Path) -> None:
    runner, recorder, _, _ = await started(tmp_path, turns=["schedule"], wake_delay=0.3)
    await runner.send("/schedule in a while")
    await wait_for(lambda: recorder.events("turn_completed"))
    assert releasable(runner) is False
    await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
    assert releasable(runner) is True
    await runner.close()


async def test_commands_are_agy_s_and_run_as_the_text_of_a_turn(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["compact"])
    # The list is a notification that follows the session's opening.
    await wait_for(lambda: runner._commands)
    listed = await runner.commands()
    assert [command.name for command in listed] == ["goal", "compact", "schedule"]
    assert all(command.argument is None for command in listed)
    with pytest.raises(RcError) as caught:
        await runner.command("logout", None, "c-0")
    assert caught.value.code == "not_found"
    await runner.command("compact", None, "c-1")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("user_message")[0]["text"] == "/compact"
    assert peer.calls("session/prompt")[0]["prompt"] == [{"type": "text", "text": "/compact"}]
    assert not recorder.events("assistant_text"), "agy compacts with no words at all"


async def test_a_prompt_agy_fails_is_an_error_in_its_words_without_paths(tmp_path: Path) -> None:
    runner, recorder, _, _ = await started(tmp_path, turns=["fail"])
    await runner.send("Go.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    [error] = recorder.events("error")
    assert error["message"] == "Model overloaded while reading a.py"
    assert recorder.events("turn_completed")[0]["stop_reason"] == "error"


async def test_agy_exiting_in_a_turn_ends_it_in_error(tmp_path: Path) -> None:
    runner, recorder, _, _ = await started(tmp_path, turns=["exit"])
    await runner.send("Go.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert [event["message"] for event in recorder.events("error")] == [
        "Antigravity stopped before the turn finished."
    ]
    assert recorder.events("turn_completed")[0]["stop_reason"] == "error"
    assert not runner.busy


async def test_nobody_signed_in_is_said_in_the_device_s_words(tmp_path: Path) -> None:
    binary, peer = fake_agy(tmp_path, signed_out=True)
    runner = runner_for(entry_for(tmp_path, Recorder()), binary)
    with pytest.raises(RcError) as caught:
        await runner.start()
    assert (caught.value.code, caught.value.message) == ("agent_unavailable", SIGNED_OUT)
    assert "authenticate" not in peer.methods()


async def test_an_agy_older_than_1_5_13_is_refused(tmp_path: Path) -> None:
    binary, _ = fake_agy(tmp_path, version="1.5.12")
    runner = runner_for(entry_for(tmp_path, Recorder()), binary)
    with pytest.raises(RcError) as caught:
        await runner.start()
    assert caught.value.code == "agent_unavailable"
    assert caught.value.message == (
        "Antigravity CLI 1.5.13 or later is needed; this computer has 1.5.12. Run agy update there."
    )


async def test_a_backend_that_cannot_start_is_said_in_agy_s_words(tmp_path: Path) -> None:
    binary, _ = fake_agy(tmp_path, backend_fails=True)
    runner = runner_for(entry_for(tmp_path, Recorder()), binary)
    with pytest.raises(RcError) as caught:
        await runner.start()
    assert caught.value.code == "agent_unavailable"
    assert caught.value.message.startswith("Failed to start Antigravity: language server")


async def test_files_go_as_an_image_embedded_text_and_a_link(tmp_path: Path) -> None:
    runner, recorder, peer, _ = await started(tmp_path, turns=["read"])
    pixel = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    uploads = [
        {"name": "shot.png", "mime": "image/png", "data_base64": pixel},
        {
            "name": "notes.txt",
            "mime": "text/plain",
            "data_base64": base64.b64encode(b"MANGO").decode(),
        },
        {"name": "blob.bin", "mime": "application/octet-stream", "data_base64": "AAEC"},
    ]
    await runner.send("Look.", uploads)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    blocks = peer.calls("session/prompt")[0]["prompt"]
    assert [block["type"] for block in blocks] == ["text", "image", "resource", "resource_link"]
    assert blocks[1] == {"type": "image", "mimeType": "image/png", "data": pixel}
    assert blocks[2]["resource"]["text"] == "MANGO"
    assert blocks[3]["name"] == "blob.bin" and blocks[3]["uri"].startswith("file://")
    shown = recorder.events("user_message")[0]["attachments"]
    assert [(item["name"], item["size"]) for item in shown] == [
        ("shot.png", 8),
        ("notes.txt", 5),
        ("blob.bin", 3),
    ]
    assert not recorder.events("approval"), "the read of its own file asks nobody"
    assert peer.replies()[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow"}}
    assert "READ allow" in recorder.events("assistant_text")[-1]["text"]
    assert all("file://" not in str(item) for item in shown), "no path reaches an app"


async def test_a_cancel_that_does_not_settle_restarts_agy_and_ends_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "DRAIN_TIMEOUT", 0.3)
    listed = [
        {
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "title": "t",
            "updatedAt": "2026-11-22T11:19:33Z",
        }
    ]
    runner, recorder, peer, _ = await started(tmp_path, turns=["stuck"], list=listed)
    await runner.send("Go.")
    await wait_for(lambda: peer.calls("session/prompt"))
    assert await runner.interrupt() is True
    await runner.close()

    assert [e["text"] for e in recorder.events("notice")] == [
        "the agent did not stop in time",
        "reconnecting to the agent",
    ]
    assert recorder.events("turn_completed")[0]["stop_reason"] == "interrupted"
    assert [start["argv"] for start in peer.starts()] == [["acp"], ["acp"]]
    assert peer.calls("session/resume")[0]["sessionId"] == SESSION


async def test_the_next_message_after_agy_exited_starts_it_again(tmp_path: Path) -> None:
    listed = [
        {
            "sessionId": SESSION,
            "cwd": str(tmp_path),
            "title": "t",
            "updatedAt": "2026-11-22T11:19:33Z",
        }
    ]
    runner, recorder, peer, _ = await started(tmp_path, turns=["exit", "ok"], list=listed)
    await runner.send("Go.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.send("Again.")
    await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
    await runner.close()
    await runner._service.lister.close()

    assert [e["stop_reason"] for e in recorder.events("turn_completed")] == ["error", "completed"]
    acp = [start for start in peer.starts() if start["argv"] == ["acp"]]
    assert len(acp) == 3, "the session's process, the list process, and the session's again"
    assert peer.calls("session/resume")[0]["sessionId"] == SESSION
