"""Translation of Grok's ACP updates, against traffic recorded from the real agent.

`tests/fixtures/grok/turn.jsonl` and `resume.jsonl` were captured from
`agent agent stdio` (grok 1.0.25); `terminal-updates.jsonl` is the
`updates.jsonl` that same session left on disk. Tool calls and plans are not in
the recording — it holds a single prompt — so those rows
are built here from the shapes the real logs show, and are marked as such.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rc_client import attachments
from rc_client.agents.base import Emit
from rc_client.agents.grok.translate import SETTINGS, GrokTranslator
from rc_client.ids import block_uuid
from tests.helpers import event_validator

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"
SESSION = "01a09b4f-1b25-7e92-b96b-356d292ff2d0"


def rows(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def replay(name: str, **kwargs: Any) -> list[Emit]:
    translator = GrokTranslator(**kwargs)
    emits: list[Emit] = []
    for row in rows(name):
        emits.extend(translator.notification(str(row.get("method")), row.get("params") or {}))
    return emits


def kinds(emits: list[Emit]) -> list[str]:
    return [emit.kind for emit in emits]


def final(emits: list[Emit], kind: str) -> dict[str, Any]:
    return [emit.fields for emit in emits if emit.kind == kind and not emit.delta][-1]


def update(kind: str, body: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "sessionId": SESSION,
        "update": {"sessionUpdate": kind, **body},
        "_meta": {"eventId": f"{SESSION}-1", "agentTimestampMs": 1789312054818, **(meta or {})},
    }


# ------------------------------------------------------------------ recorded


def test_a_recorded_turn_streams_thinking_then_text_then_completes() -> None:
    emits = replay("turn.jsonl", mirror_user_messages=False)
    assert kinds(emits)[0] == "thinking"
    assert "assistant_text" in kinds(emits)
    # `current_mode_update` follows the turn: the probe set plan mode at the end.
    assert "turn_completed" in kinds(emits)
    # Every chunk is a delta and each block is closed once with the whole text.
    thinking = final(emits, "thinking")
    assert thinking["done"] is True
    assert thinking["text"].startswith("The user wants me to reply")
    text = final(emits, "assistant_text")
    assert text == {"block_id": text["block_id"], "text": "OK", "done": True}
    assert sum(1 for emit in emits if emit.kind == "assistant_text" and emit.delta) == 1


def test_a_recorded_turn_reports_tokens_and_the_real_cost() -> None:
    completion = final(replay("turn.jsonl"), "turn_completed")
    assert completion["stop_reason"] == "completed"
    assert completion["duration_ms"] > 0
    usage = completion["usage"]
    assert usage["input_tokens"] == 25345
    assert usage["output_tokens"] == 37
    assert usage["total_tokens"] == 25382
    # 143072000 ticks at 1e10 ticks to the dollar.
    assert usage["cost_usd"] == 0.0143072


def test_the_replay_a_resume_sends_is_dropped() -> None:
    emits = replay("resume.jsonl")
    # `session/load` replays the first turn with `isReplay`; only the second
    # turn's own updates may become events.
    assert [emit.fields.get("text") for emit in emits if emit.kind == "user_message"] == []
    assert sum(1 for emit in emits if emit.kind == "turn_completed") == 1


def test_a_resume_reports_the_model_and_effort_the_agent_switched_to() -> None:
    settings = [emit.fields for emit in replay("resume.jsonl") if emit.kind == SETTINGS]
    assert {"model": "grok-4.6", "effort": "low"} in settings


def test_the_update_log_a_terminal_writes_carries_the_same_shapes() -> None:
    emits = replay("terminal-updates.jsonl")
    message = [emit.fields for emit in emits if emit.kind == "user_message"]
    assert message and message[0]["source"] == "terminal"
    assert message[0]["text"] == "Reply with exactly OK"
    assert final(emits, "assistant_text")["text"] == "OK"
    assert final(emits, "turn_completed")["stop_reason"] == "completed"


def test_a_driven_session_never_mirrors_its_own_prompt() -> None:
    emits = replay("terminal-updates.jsonl", mirror_user_messages=False)
    assert "user_message" not in kinds(emits)


def test_a_logged_prompt_with_files_shows_their_names_and_never_their_paths() -> None:
    """A49: `updates.jsonl` keeps the prompt as Grok got it, paths and all."""
    written = attachments.materialise(SESSION, [{"name": "shot.png", "data_base64": "aGVsbG8="}])
    prompt = attachments.describe("look", written)
    row = update("user_message_chunk", {"content": {"type": "text", "text": prompt}})
    [emit] = GrokTranslator().notification("session/update", row)
    assert emit.fields == {
        "block_id": f"user:{SESSION}-1",
        "text": "look",
        "source": "terminal",
        "attachments": [{"name": "shot.png", "mime": "image/png", "size": 5}],
    }


def test_hook_rows_are_not_events() -> None:
    translator = GrokTranslator()
    hook = update("hook_execution", {"event_name": "pre_tool_use", "runs": []})
    assert translator.notification("_x.ai/session/update", hook) == []
    assert translator.notification("_x.ai/queue/changed", {"entries": []}) == []
    assert translator.notification("session/update", update("something_new", {})) == []


# -------------------------------------------------------------- constructed


def test_a_shell_tool_call_becomes_a_running_then_a_finished_block() -> None:
    translator = GrokTranslator()
    started = translator.notification(
        "session/update",
        update(
            "tool_call",
            {
                "toolCallId": "call-1",
                "title": "run_terminal_command",
                "rawInput": {"command": "pytest -q\nsecond line", "description": "run the tests"},
                "_meta": {
                    "x.ai/tool": {
                        "name": "run_terminal_command",
                        "kind": "execute",
                        "namespace": "grok_build",
                    }
                },
            },
        ),
    )
    assert len(started) == 1
    fields = started[0].fields
    assert fields["tool"] == "run_terminal_command"
    assert fields["tool_kind"] == "shell"
    assert fields["title"] == "pytest -q"
    assert fields["status"] == "running"
    assert fields["input"]["command"].startswith("pytest -q")
    assert "ended_at" not in fields

    finished = translator.notification(
        "session/update",
        update(
            "tool_call_update",
            {
                "toolCallId": "call-1",
                "status": "completed",
                "kind": "execute",
                "locations": [{"path": "/repo/tests"}],
                "content": [{"type": "content", "content": {"type": "text", "text": "2 passed"}}],
                "rawOutput": {"output": [1, 2, 3], "output_for_prompt": "2 passed", "exit_code": 0},
            },
            {"agentTimestampMs": 1789312055818},
        ),
    )
    fields = finished[0].fields
    assert fields["status"] == "succeeded"
    assert fields["output"] == "2 passed"
    assert fields["summary"] == "exit 0"
    assert fields["input"]["locations"] == [{"path": "/repo/tests"}]
    # The byte array Grok also sends is never published.
    assert "[1, 2, 3]" not in json.dumps(fields)
    assert fields["duration_ms"] == 1000


def test_an_mcp_tool_is_reported_as_mcp() -> None:
    translator = GrokTranslator()
    emits = translator.notification(
        "session/update",
        update(
            "tool_call",
            {
                "toolCallId": "call-2",
                "title": "context7.query",
                "_meta": {"x.ai/tool": {"name": "query", "kind": "other", "namespace": "context7"}},
            },
        ),
    )
    assert emits[0].fields["tool_kind"] == "mcp"


def test_an_edit_carries_the_unified_patch_apps_render() -> None:
    translator = GrokTranslator()
    emits = translator.notification(
        "session/update",
        update(
            "tool_call_update",
            {
                "toolCallId": "call-3",
                "status": "completed",
                "kind": "edit",
                "content": [
                    {
                        "type": "diff",
                        "path": "rc_client/main.py",
                        "oldText": "one\ntwo\n",
                        "newText": "one\ntwo\nthree\n",
                    }
                ],
            },
        ),
    )
    diff = emits[0].fields["diff"]
    assert diff["path"] == "rc_client/main.py"
    assert diff["additions"] == 1
    assert diff["deletions"] == 0
    assert "+three" in diff["patch"]


def test_a_plan_becomes_todos() -> None:
    translator = GrokTranslator()
    emits = translator.notification(
        "session/update",
        update(
            "plan",
            {
                "entries": [
                    {"content": "read the code", "status": "completed"},
                    {"content": "write the test", "status": "in_progress"},
                    {"content": "ship", "status": "nonsense"},
                ]
            },
        ),
    )
    assert emits[0].fields["items"] == [
        {"id": "0", "text": "read the code", "status": "completed"},
        {"id": "1", "text": "write the test", "status": "in_progress"},
        {"id": "2", "text": "ship", "status": "pending"},
    ]


def test_a_permission_mode_change_is_reported_as_a_setting() -> None:
    translator = GrokTranslator()
    emits = translator.notification(
        "session/update", update("current_mode_update", {"currentModeId": "plan"})
    )
    assert emits == [Emit(SETTINGS, {"permission_mode": "plan"})]


def test_every_published_event_validates_against_the_schema() -> None:
    validator = event_validator()
    if validator is None:
        return
    for name in ("turn.jsonl", "resume.jsonl", "terminal-updates.jsonl"):
        for seq, emit in enumerate(replay(name)):
            if emit.kind == SETTINGS:
                continue
            payload: dict[str, Any] = {"seq": seq + 1, "ts": 1789312054818, "kind": emit.kind}
            payload.update(emit.fields)
            # The channel rewrites every block id as a uuid before publishing.
            if isinstance(payload.get("block_id"), str):
                payload["block_id"] = block_uuid(str(payload["block_id"]))
            if emit.delta:
                payload["done"] = False
            if emit.kind == "turn_completed":
                payload["turn_id"] = "0b6d3a1e-6f6c-4a3f-9a3a-2a34a7f7e3a1"
            validator.validate(payload)


def test_an_error_quoting_a_file_names_it_even_where_it_is_cut() -> None:
    """The paths come off before the 2000-character cut, so none is left half there."""
    written = attachments.materialise(SESSION, [{"name": "shot.png", "data_base64": "aGVsbG8="}])
    said = "x" * 1990 + f"{written[0].path} could not be read"
    [emit] = GrokTranslator().notification("session/update", update("error", {"message": said}))
    assert emit.fields["message"] == ("x" * 1990 + "shot.png could not be read")[:2000]
