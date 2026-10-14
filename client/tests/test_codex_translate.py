"""Codex notification and rollout translation."""

from __future__ import annotations

from typing import Any

from rc_client.agents.codex.models import parse_catalog
from rc_client.agents.codex.translate import CodexTranslator, camel, normalise


def only(emits: list[Any], kind: str) -> Any:
    matching = [emit for emit in emits if emit.kind == kind]
    assert len(matching) == 1, f"expected one {kind}, got {[e.kind for e in emits]}"
    return matching[0]


def test_snake_case_rollout_keys_normalise_to_the_live_camel_case_shape() -> None:
    assert camel("unified_diff") == "unifiedDiff"
    assert camel("cwd") == "cwd"
    assert normalise({"summary_text": [{"raw_content": "x"}]}) == {
        "summaryText": [{"rawContent": "x"}]
    }


def test_agent_message_delta_streams_into_a_block() -> None:
    translator = CodexTranslator()
    emits = translator.notification(
        "item/agentMessage/delta",
        {"itemId": "msg_1", "delta": "Hel", "threadId": "t", "turnId": "u"},
    )
    assert emits[0].kind == "assistant_text"
    assert emits[0].delta
    assert emits[0].fields == {"block_id": "msg_1", "delta": "Hel"}


def test_reasoning_deltas_become_thinking() -> None:
    translator = CodexTranslator()
    for method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
        emits = translator.notification(method, {"itemId": "rs_1", "delta": "why"})
        assert emits[0].kind == "thinking"
        assert emits[0].delta


def test_command_execution_items_become_shell_tool_rows() -> None:
    translator = CodexTranslator()
    started = translator.notification(
        "item/started",
        {
            "startedAtMs": 1000,
            "item": {
                "type": "commandExecution",
                "id": "exec-1",
                "command": "pytest -q",
                "cwd": "/repo",
                "status": "inProgress",
            },
        },
    )
    row = only(started, "tool_call")
    assert row.fields["tool_kind"] == "shell"
    assert row.fields["title"] == "pytest -q"
    assert row.fields["status"] == "running"

    completed = translator.notification(
        "item/completed",
        {
            "completedAtMs": 2000,
            "item": {
                "type": "commandExecution",
                "id": "exec-1",
                "command": "pytest -q",
                "status": "completed",
                "exitCode": 1,
                "aggregatedOutput": "1 failed",
            },
        },
    )
    done = only(completed, "tool_call")
    assert done.fields["status"] == "failed"
    assert done.fields["output"] == "1 failed"
    assert done.fields["summary"] == "exit 1"
    assert done.fields["duration_ms"] == 1000


def test_command_output_deltas_republish_the_block_with_more_output() -> None:
    translator = CodexTranslator()
    translator.notification(
        "item/started",
        {
            "startedAtMs": 1,
            "item": {
                "type": "commandExecution",
                "id": "exec-2",
                "command": "ls",
                "status": "inProgress",
            },
        },
    )
    first = translator.notification(
        "item/commandExecution/outputDelta", {"itemId": "exec-2", "delta": "a"}
    )
    second = translator.notification(
        "item/commandExecution/outputDelta", {"itemId": "exec-2", "delta": "b"}
    )
    assert first[0].fields["output"] == "a"
    assert second[0].fields["output"] == "ab"
    assert second[0].fields["status"] == "running"


def test_file_change_items_carry_a_diff_from_either_payload_shape() -> None:
    translator = CodexTranslator()
    live = translator.notification(
        "item/completed",
        {
            "completedAtMs": 5,
            "item": {
                "type": "fileChange",
                "id": "fc-1",
                "status": "completed",
                "changes": [
                    {"path": "a.py", "diff": "@@\n+one\n-two\n", "kind": {"type": "update"}}
                ],
            },
        },
    )
    diff = only(live, "tool_call").fields["diff"]
    assert diff["path"] == "a.py"
    assert diff["additions"] == 1
    assert diff["deletions"] == 1

    rollout = CodexTranslator().item(
        {
            "type": "FileChange",
            "id": "fc-2",
            "status": "completed",
            "changes": {"/repo/b.py": {"type": "update", "unified_diff": "@@\n+x\n"}},
        },
        True,
    )
    assert only(rollout, "tool_call").fields["diff"]["path"] == "/repo/b.py"


def test_plan_updates_become_a_todos_snapshot() -> None:
    translator = CodexTranslator()
    emits = translator.notification(
        "turn/plan/updated",
        {"plan": [{"step": "one", "status": "completed"}, {"step": "two", "status": "inProgress"}]},
    )
    items = only(emits, "todos").fields["items"]
    assert items == [
        {"id": "0", "text": "one", "status": "completed"},
        {"id": "1", "text": "two", "status": "in_progress"},
    ]


def test_turn_completed_maps_status_and_carries_usage() -> None:
    translator = CodexTranslator()
    translator.notification(
        "thread/tokenUsage/updated",
        {
            "threadId": "t",
            "turnId": "u",
            "tokenUsage": {
                "total": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "modelContextWindow": 272000,
            },
        },
    )
    emits = translator.notification(
        "turn/completed",
        {
            "threadId": "t",
            "turn": {"id": "u", "status": "completed", "durationMs": 900, "items": []},
        },
    )
    completed = only(emits, "turn_completed")
    assert completed.fields["stop_reason"] == "completed"
    assert completed.fields["usage"]["total_tokens"] == 120
    assert completed.fields["usage"]["context_window"] == 272000
    assert "cost_usd" not in completed.fields["usage"]


def test_interrupted_and_failed_turns_map_to_their_stop_reasons() -> None:
    for status, expected in (("interrupted", "interrupted"), ("failed", "error")):
        emits = CodexTranslator().notification(
            "turn/completed", {"turn": {"id": "u", "status": status, "items": []}}
        )
        assert only(emits, "turn_completed").fields["stop_reason"] == expected


def test_rollout_pascal_case_items_translate_like_live_ones() -> None:
    translator = CodexTranslator()
    emits = translator.item(
        {"type": "AgentMessage", "id": "msg_9", "content": [{"type": "Text", "text": "done"}]},
        True,
    )
    assert only(emits, "assistant_text").fields == {
        "block_id": "msg_9",
        "text": "done",
        "done": True,
    }
    user = translator.item(
        {"type": "UserMessage", "id": "um_1", "content": [{"type": "text", "text": "go"}]}, True
    )
    assert only(user, "user_message").fields["source"] == "terminal"


def test_the_live_stream_does_not_mirror_prompts_the_daemon_sent() -> None:
    """The adapter already published that block; mirroring it again duplicates the message."""
    translator = CodexTranslator(mirror_user_messages=False)
    item = {"type": "UserMessage", "id": "um_2", "content": [{"type": "text", "text": "go"}]}
    assert translator.item(item, True) == []


def test_model_catalog_parsing_and_effort_clamping() -> None:
    catalog = parse_catalog(
        [
            {
                "id": "gpt-6",
                "displayName": "GPT-6",
                "isDefault": True,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low"},
                    {"reasoningEffort": "medium"},
                    {"reasoningEffort": "high"},
                ],
            },
            {"id": "hidden", "hidden": True, "displayName": "Hidden"},
            {
                "id": "small",
                "displayName": "Small",
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            },
        ]
    )
    assert [choice.id for choice in catalog.models] == ["gpt-6", "small"]
    assert catalog.default_model == "gpt-6"
    assert catalog.default_effort == "medium"
    assert [choice.id for choice in catalog.efforts] == ["low", "medium", "high"]
    assert catalog.clamp_effort("gpt-6", "high") == "high"
    assert catalog.clamp_effort("small", "max") == "low"
    assert catalog.clamp_effort("gpt-6", None) is None
