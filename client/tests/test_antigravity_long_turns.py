"""What `agy` 1.5.15 and 1.5.16 changed, replayed as `agy` sends it (A64).

Since 1.5.15 a background command keeps the prompt open: agy's first words, the command's result,
agy's reply to it, and only then the answer, so the turn is one and no turn of agy's own follows.
Since 1.5.16 a sub-agent's permission request reaches the client under the parent's session id,
and the prompt answers after a silent tail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import runner as runner_module
from rc_client.agents.antigravity.options import BYPASS
from tests.agy_helpers import SESSION, Recorder, entry_for, fake_agy, runner_for, wait_for


@pytest.fixture(autouse=True)
def quick_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "WAKE_QUIET", 0.2)


async def started(
    tmp_path: Path, mode: str = "default", **config: Any
) -> tuple[runner_module.AntigravityRunner, Recorder, Any]:
    binary, peer = fake_agy(tmp_path, **config)
    recorder = Recorder()
    runner = runner_for(entry_for(tmp_path, recorder, permission_mode=mode), binary)
    await runner.start()
    return runner, recorder, peer


def words(recorder: Recorder) -> list[tuple[str, str]]:
    """Each finished text block: its id and its words."""
    return [
        (event["block_id"], event["text"])
        for event in recorder.events("assistant_text")
        if event.get("done")
    ]


async def answer_the_card(runner: runner_module.AntigravityRunner, recorder: Recorder) -> None:
    await wait_for(lambda: recorder.events("approval"))
    card = recorder.events("approval")[0]
    assert await runner.approve(card["request_id"], "allow", None) is True


async def test_a_background_command_keeps_the_turn_and_its_reply_is_a_block_of_its_own(
    tmp_path: Path,
) -> None:
    runner, recorder, _ = await started(tmp_path, turns=["long"])
    await runner.send("Run sleep 15 and tell me what it printed.")
    await answer_the_card(runner, recorder)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert [event["trigger"] for event in recorder.events("turn_started")] == ["remote"]
    [ended] = recorder.events("turn_completed")
    assert ended["stop_reason"] == "completed"
    assert ended["usage"]["input_tokens"] == 15249, "the tokens of the whole prompt"
    [(first, before), (second, after)] = words(recorder)
    assert first != second, "two replies, two bubbles"
    assert before == "I have started running the command in the background."
    assert after.strip() == "The command printed `slept`."
    states = recorder.states()
    assert states.count("idle") == 1 and states[-1] == "idle", "idle only once the prompt answered"


async def test_stop_during_the_background_wait_ends_the_turn(tmp_path: Path) -> None:
    runner, recorder, peer = await started(
        tmp_path, turns=["long"], background_delay=30, wake_delay=30
    )
    await runner.send("Run sleep 15 and tell me what it printed.")
    await answer_the_card(runner, recorder)
    await wait_for(lambda: words(recorder) or recorder.events("assistant_text"))
    assert await runner.interrupt() is True
    await wait_for(lambda: recorder.events("turn_completed"), timeout=5)
    await runner.close()
    assert recorder.events("turn_completed")[0]["stop_reason"] == "interrupted"
    assert peer.calls("session/cancel") == [{"sessionId": SESSION}]


async def test_a_sub_agent_s_request_is_a_card_answered_by_its_id(tmp_path: Path) -> None:
    runner, recorder, peer = await started(tmp_path, turns=["subagent"])
    await runner.send("Have a subagent run echo sub-ok > sub.txt.")
    await wait_for(lambda: recorder.events("approval"))
    card = recorder.events("approval")[0]
    assert card["title"] == "echo sub-ok > sub.txt", "the sub-agent's command, on the parent"
    assert await runner.approve(card["request_id"], "allow", None) is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert peer.replies()[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow"}}
    finished = {e["title"] for e in recorder.events("tool_call") if e["status"] == "succeeded"}
    assert "echo sub-ok > sub.txt" in finished, "the sub-agent's row shows on the parent"
    assert len(recorder.events("turn_completed")) == 1, "one turn, the silent tail included"
    assert words(recorder)[-1][1].strip() == "The `manage_task` tool lists 0 tasks."


async def test_bypass_permissions_approves_a_sub_agent_s_request_too(tmp_path: Path) -> None:
    runner, recorder, peer = await started(tmp_path, mode=BYPASS.id, turns=["subagent"])
    await runner.send("Have a subagent run echo sub-ok > sub.txt.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("approval") == [], "no card (A67)"
    assert peer.replies()[0]["result"] == {"outcome": {"outcome": "selected", "optionId": "allow"}}
