"""Amendment A60: a brief history page.

An app opening a session asks for `brief: true`, and every tool call in the page
comes cut short: at most 1 KiB of input, no output, no patch, each cut marked.
Everything else is the page it always was, and `session.block` still returns the
whole block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.errors import RcError
from rc_client.events import BRIEF_INPUT_BYTES, brief_event
from tests.helpers import load_fixture
from tests.test_hub import add_session, build_hub


def tool_call(**fields: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "seq": 9,
        "ts": 1,
        "kind": "tool_call",
        "block_id": "5ca335d3-3c57-4026-9358-49567115efde",
        "first_seq": 7,
        "tool": "Bash",
        "tool_kind": "shell",
        "title": "pytest -q",
        "status": "succeeded",
        "started_at": 1,
    }
    event.update(fields)
    return event


def test_a_brief_tool_call_keeps_its_row_and_drops_its_body() -> None:
    event = tool_call(
        input={"command": "x" * 5000, "timeout": 120},
        output="lots of output",
        diff={"path": "a.py", "additions": 4, "deletions": 2, "patch": "--- a\n+++ b\n"},
        summary="2 failed",
    )
    brief = brief_event(event)

    assert len(json.dumps(brief["input"]).encode("utf-8")) <= BRIEF_INPUT_BYTES
    assert brief["input_truncated"] is True
    assert "output" not in brief
    assert brief["output_truncated"] is True
    kept = {"path": "a.py", "additions": 4, "deletions": 2, "patch_truncated": True}
    assert brief["diff"] == kept
    for key in ("seq", "kind", "block_id", "first_seq", "tool", "tool_kind", "title", "status"):
        assert brief[key] == event[key]
    assert brief["summary"] == "2 failed"
    # The event itself is untouched: the stored block is what `session.block` returns.
    assert event["output"] == "lots of output"


def test_what_a_tool_call_never_had_is_not_marked_cut() -> None:
    brief = brief_event(tool_call(input={"command": "ls"}, output=""))
    assert brief["input"] == {"command": "ls"}
    assert "input_truncated" not in brief
    assert "output" not in brief
    assert "output_truncated" not in brief
    assert "diff" not in brief


def test_every_other_event_is_left_as_it_is() -> None:
    message = {"seq": 2, "ts": 1, "kind": "user_message", "block_id": "b", "text": "y" * 4000}
    assert brief_event(message) is message


async def test_the_device_pages_briefly_only_when_asked(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    page = load_fixture("history/page.json")
    for event in page["result"]["events"]:
        registry.store_event("sess-1", event, event)

    full = await hub.history({"session_id": "sess-1", "limit": 200})
    assert full["events"] == page["result"]["events"]
    brief = await hub.history({"session_id": "sess-1", "limit": 200, "brief": True})
    assert brief == load_fixture("history/page.brief.json")["result"]
    assert await hub.history({"session_id": "sess-1", "limit": 200, "brief": False}) == full
    registry.close()


async def test_brief_must_be_a_boolean(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    with pytest.raises(RcError) as refused:
        await hub.history({"session_id": "sess-1", "brief": "yes"})
    assert refused.value.code == "bad_request"
    registry.close()


async def test_the_whole_block_is_still_there(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    event = tool_call(output="every line of it")
    registry.store_event("sess-1", event, event)
    brief = await hub.history({"session_id": "sess-1", "brief": True})
    assert "output" not in brief["events"][0]
    block = await hub.block({"session_id": "sess-1", "block_id": event["block_id"]})
    assert block["event"]["output"] == "every line of it"
    registry.close()
