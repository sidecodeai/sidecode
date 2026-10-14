"""Translation of pi's RPC events into protocol events.

pi needs a provider key this machine has not given it, so no live turn was
recorded: every row under `tests/fixtures/pi/` is written from the shapes pi's
own shipped documentation gives (`docs/rpc.md`, `docs/json.md`, 0.85.1), and the
tool argument and result shapes from the package's own type declarations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rc_client.agents.base import Emit
from rc_client.agents.pi.translate import QUEUE, PiTranslator
from tests.helpers import event_validator

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pi"


def rows(name: str) -> list[dict[str, Any]]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def replay(name: str) -> list[Emit]:
    translator = PiTranslator()
    emits: list[Emit] = []
    for row in rows(name):
        emits.extend(translator.event(row))
    return emits


def kinds(emits: list[Emit]) -> list[str]:
    return [emit.kind for emit in emits]


def blocks(emits: list[Emit], kind: str) -> list[dict[str, Any]]:
    return [emit.fields for emit in emits if emit.kind == kind and not emit.delta]


def test_a_turn_streams_thinking_then_text_then_the_tool_it_ran() -> None:
    emits = replay("turn.jsonl")
    assert kinds(emits)[-1] == "turn_completed"
    thinking = blocks(emits, "thinking")[-1]
    assert thinking["done"] is True
    assert thinking["text"] == "They want the two letters back."
    texts = blocks(emits, "assistant_text")
    assert [block["text"] for block in texts] == ["OK", "Listed the directory."]
    assert all(block["done"] for block in texts)
    # Two messages, so the second block is its own bubble rather than an append.
    assert texts[0]["block_id"] != texts[1]["block_id"]


def test_every_delta_carries_only_what_was_added() -> None:
    emits = [emit for emit in replay("turn.jsonl") if emit.delta]
    assert [emit.fields["delta"] for emit in emits if emit.kind == "assistant_text"] == [
        "O",
        "K",
        "Listed the directory.",
    ]
    assert all("text" not in emit.fields for emit in emits)


def test_a_tool_call_is_published_running_then_finished() -> None:
    calls = blocks(replay("turn.jsonl"), "tool_call")
    assert [call["status"] for call in calls] == ["running", "running", "succeeded"]
    assert {call["block_id"] for call in calls} == {"call_abc123"}
    assert calls[0]["tool"] == "bash"
    assert calls[0]["tool_kind"] == "shell"
    assert calls[0]["title"] == "ls -la"
    assert calls[0]["input"] == {"command": "ls -la"}
    # An update carries everything printed so far, not the delta.
    assert calls[1]["output"] == "total 8\n"
    assert calls[2]["output"].startswith("total 8\ndrwxr-xr-x")
    assert calls[2]["ended_at"] >= calls[2]["started_at"]


def test_an_edit_publishes_the_patch_pi_applied() -> None:
    calls = blocks(replay("tools.jsonl"), "tool_call")
    edit = [call for call in calls if call["tool"] == "edit"][-1]
    assert edit["tool_kind"] == "edit"
    assert edit["title"] == "/repo/rc_client/auth.py"
    assert edit["diff"]["path"] == "/repo/rc_client/auth.py"
    assert edit["diff"]["additions"] == 1
    assert edit["diff"]["deletions"] == 1
    assert edit["diff"]["patch"].startswith("--- a/rc_client/auth.py")


def test_a_failed_tool_and_an_extension_tool_are_both_readable() -> None:
    calls = blocks(replay("tools.jsonl"), "tool_call")
    grep = [call for call in calls if call["tool"] == "grep"][-1]
    assert grep["status"] == "failed"
    assert grep["tool_kind"] == "search"
    assert grep["title"] == "should_refresh"
    extension = [call for call in calls if call["tool"] == "ask_question"][-1]
    # pi ships no tool by that name, so it is somebody's extension.
    assert extension["tool_kind"] == "other"
    assert extension["title"] == "Which branch?"


def test_the_queue_snapshot_is_handed_to_the_adapter_rather_than_published() -> None:
    translator = PiTranslator()
    emits = translator.event(
        {"type": "queue_update", "steering": ["and run the tests"], "followUp": []}
    )
    assert kinds(emits) == [QUEUE]
    assert emits[0].fields == {"steering": ["and run the tests"], "follow_up": []}


def test_a_stopped_message_ends_the_turn_as_interrupted() -> None:
    translator = PiTranslator()
    translator.event({"type": "message_start", "message": {"role": "assistant"}})
    translator.event({"type": "message_end", "message": {"stopReason": "aborted"}})
    emits = translator.event({"type": "agent_settled"})
    assert blocks(emits, "turn_completed")[0]["stop_reason"] == "interrupted"


def test_a_failed_message_reports_what_went_wrong() -> None:
    translator = PiTranslator()
    emits = translator.event(
        {
            "type": "message_end",
            "message": {"stopReason": "error", "errorMessage": "overloaded_error"},
        }
    )
    assert blocks(emits, "error")[0]["message"] == "overloaded_error"
    assert (
        blocks(translator.event({"type": "agent_settled"}), "turn_completed")[0]["stop_reason"]
        == "error"
    )


def test_compaction_and_a_retry_are_system_lines() -> None:
    translator = PiTranslator()
    compacted = translator.event(
        {"type": "compaction_end", "reason": "threshold", "result": {}, "aborted": False}
    )
    assert compacted[0].fields["level"] == "warn"
    assert "compacted" in compacted[0].fields["text"]
    aborted = translator.event({"type": "compaction_end", "result": None, "aborted": True})
    assert aborted[0].fields["level"] == "info"
    failed = translator.event({"type": "compaction_end", "errorMessage": "quota exceeded"})
    assert failed[0].fields == {"level": "error", "text": "quota exceeded"}
    retry = translator.event({"type": "auto_retry_start", "attempt": 1})
    assert retry[0].fields["level"] == "warn"


def test_an_unfinished_block_is_closed_when_the_session_is_torn_down() -> None:
    translator = PiTranslator()
    translator.event({"type": "message_start", "message": {"role": "assistant"}})
    translator.event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "half"},
        }
    )
    closed = translator.close_streams()
    assert closed[0].fields == {
        "block_id": closed[0].fields["block_id"],
        "text": "half",
        "done": True,
    }
    assert translator.close_streams() == []


def test_an_unknown_event_is_ignored_rather_than_guessed_at() -> None:
    translator = PiTranslator()
    assert translator.event({"type": "summarization_retry_scheduled"}) == []
    assert translator.event({"type": "bash_execution_update", "delta": "x"}) == []


def test_every_event_a_turn_publishes_validates() -> None:
    validator = event_validator()
    if validator is None:
        return
    seq = 0
    for emit in replay("turn.jsonl") + replay("tools.jsonl"):
        if emit.kind in {QUEUE, "turn_completed"}:
            continue
        seq += 1
        fields = dict(emit.fields)
        block_id = fields.get("block_id")
        if isinstance(block_id, str):
            # The channel hashes an adapter's own key into a uuid before it
            # goes on the wire; the schema only ever sees the hashed form.
            fields["block_id"] = f"00000000-0000-4000-8000-{seq:012d}"
        if emit.delta:
            fields["done"] = False
        validator.validate({"seq": seq, "ts": 1789312054818, "kind": emit.kind, **fields})
