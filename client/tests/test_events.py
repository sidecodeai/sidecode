"""Event bounds, truncation and history classification (PROTOCOL §4 and §8)."""

from __future__ import annotations

import json

from rc_client.events import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_PATCH_BYTES,
    bound_event,
    bound_object,
    dedup_key,
    should_store,
    truncate_text,
)


def test_truncate_text_keeps_characters_whole() -> None:
    value = "é" * 100
    result, truncated = truncate_text(value, 40)
    assert truncated
    assert len(result.encode("utf-8")) <= 40
    result.encode("utf-8").decode("utf-8")


def test_truncate_text_leaves_short_values_alone() -> None:
    assert truncate_text("short", 100) == ("short", False)


def test_bound_object_shrinks_below_the_input_limit() -> None:
    payload = {"command": "x" * 40_000, "cwd": "/tmp"}
    bounded, truncated = bound_object(payload, MAX_INPUT_BYTES)
    assert truncated
    assert len(json.dumps(bounded).encode("utf-8")) <= MAX_INPUT_BYTES
    assert bounded["cwd"] == "/tmp"


def test_bound_event_marks_every_truncated_section() -> None:
    event = {
        "seq": 1,
        "ts": 0,
        "kind": "tool_call",
        "block_id": "b1",
        "input": {"command": "y" * 20_000},
        "output": "z" * 40_000,
        "diff": {"path": "a.py", "additions": 1, "deletions": 0, "patch": "+" * 80_000},
    }
    bounded = bound_event(event)
    assert bounded["input_truncated"] is True
    assert bounded["output_truncated"] is True
    assert bounded["diff"]["patch_truncated"] is True
    assert len(bounded["output"].encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert len(bounded["diff"]["patch"].encode("utf-8")) <= MAX_PATCH_BYTES


def test_bound_event_leaves_small_events_untouched() -> None:
    event = {"seq": 2, "ts": 0, "kind": "tool_call", "block_id": "b", "output": "ok"}
    assert bound_event(event) == event


def test_dedup_key_groups_blocks_and_todos() -> None:
    assert dedup_key({"kind": "tool_call", "block_id": "abc", "seq": 3}) == "block:abc"
    assert dedup_key({"kind": "todos", "seq": 4}) == "todos"
    assert dedup_key({"kind": "turn_started", "seq": 5}) == "seq:5"
    assert dedup_key({"kind": "status", "seq": 6}) is None


def test_should_store_skips_deltas_and_state_events() -> None:
    assert should_store({"kind": "assistant_text", "done": True})
    assert not should_store({"kind": "assistant_text", "delta": "hi"})
    assert not should_store({"kind": "status", "state": "idle"})
    assert not should_store({"kind": "meta", "model": "x"})
    assert not should_store({"kind": "queue", "pending": []})
    assert should_store({"kind": "tool_call", "block_id": "b"})
