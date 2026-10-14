"""Claude SDK message translation."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from rc_client.agents.base import Emit
from rc_client.agents.claude.tools import tool_kind, tool_title
from rc_client.agents.claude.translate import ClaudeTranslator, result_text


def stream(event: dict[str, Any], parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=parent)


def assistant(content: list[Any], message_id: str = "msg_1", parent: str | None = None) -> Any:
    return AssistantMessage(
        content=content, model="claude-x", message_id=message_id, parent_tool_use_id=parent
    )


def test_text_deltas_stream_into_one_block() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    translator.feed(stream({"type": "message_start", "message": {"id": "msg_9"}}))
    emits = translator.feed(
        stream(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            }
        )
    )
    assert emits == [Emit("assistant_text", {"block_id": "msg_9:0", "delta": "Hel"}, delta=True)]

    final = translator.feed(assistant([TextBlock(text="Hello")], message_id="msg_9"))
    assert final[0].kind == "assistant_text"
    assert final[0].fields == {"block_id": "msg_9:0", "text": "Hello", "done": True}
    assert not final[0].delta


def test_thinking_deltas_and_finalisation_share_a_block() -> None:
    translator = ClaudeTranslator()
    translator.feed(stream({"type": "message_start", "message": {"id": "msg_2"}}))
    delta = translator.feed(
        stream(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "thinking_delta", "thinking": "why"},
            }
        )
    )
    assert delta[0].kind == "thinking"
    assert delta[0].fields["block_id"] == "msg_2:1"
    final = translator.feed(
        assistant(
            [TextBlock(text="x"), ThinkingBlock(thinking="why not", signature="s")],
            message_id="msg_2",
        )
    )
    assert final[1].fields == {"block_id": "msg_2:1", "text": "why not", "done": True}


def test_tool_use_and_result_share_the_tool_id_as_block_id() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    call = translator.feed(
        assistant(
            [ToolUseBlock(id="toolu_1", name="Bash", input={"command": "pytest -q\nsecond line"})]
        )
    )
    assert call[0].kind == "tool_call"
    assert call[0].fields["block_id"] == "toolu_1"
    assert call[0].fields["tool_kind"] == "shell"
    assert call[0].fields["title"] == "pytest -q"
    assert call[0].fields["status"] == "running"

    result = translator.feed(
        UserMessage(content=[ToolResultBlock(tool_use_id="toolu_1", content="2 passed")])
    )
    assert result[0].fields["status"] == "succeeded"
    assert result[0].fields["output"] == "2 passed"
    assert result[0].fields["block_id"] == "toolu_1"


def test_failed_tool_results_are_marked_failed() -> None:
    translator = ClaudeTranslator()
    translator.feed(assistant([ToolUseBlock(id="t2", name="Read", input={"file_path": "/a/b.py"})]))
    result = translator.feed(
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="boom", is_error=True)])
    )
    assert result[0].fields["status"] == "failed"
    assert "diff" not in result[0].fields


def test_edit_results_carry_a_unified_diff() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    translator.feed(
        assistant(
            [
                ToolUseBlock(
                    id="t3",
                    name="Edit",
                    input={
                        "file_path": "/repo/a.py",
                        "old_string": "one\n",
                        "new_string": "two\nthree\n",
                    },
                )
            ]
        )
    )
    result = translator.feed(UserMessage(content=[ToolResultBlock(tool_use_id="t3", content="ok")]))
    diff = result[0].fields["diff"]
    assert diff["path"] == "/repo/a.py"
    assert diff["additions"] == 2
    assert diff["deletions"] == 1
    assert "+two" in diff["patch"]


def test_todowrite_becomes_a_todos_snapshot_not_a_tool_row() -> None:
    translator = ClaudeTranslator()
    emits = translator.feed(
        assistant(
            [
                ToolUseBlock(
                    id="t4",
                    name="TodoWrite",
                    input={
                        "todos": [
                            {"content": "one", "status": "completed"},
                            {"content": "two", "status": "in_progress"},
                        ]
                    },
                )
            ]
        )
    )
    assert len(emits) == 1
    assert emits[0].kind == "todos"
    assert emits[0].fields["items"][0] == {"id": "0", "text": "one", "status": "completed"}
    assert (
        translator.feed(UserMessage(content=[ToolResultBlock(tool_use_id="t4", content="ok")]))
        == []
    )


def test_subagent_blocks_carry_the_parent_block_id() -> None:
    translator = ClaudeTranslator()
    emits = translator.feed(assistant([TextBlock(text="inner")], parent="toolu_parent"))
    assert emits[0].fields["parent_block_id"] == "toolu_parent"


def test_init_system_message_reports_the_model_and_session_id() -> None:
    translator = ClaudeTranslator()
    emits = translator.feed(
        SystemMessage(subtype="init", data={"model": "claude-x", "session_id": "sess-1"})
    )
    assert emits == [Emit("meta", {"model": "claude-x"})]
    assert translator.session_id == "sess-1"


def test_result_message_becomes_turn_completed_with_usage() -> None:
    translator = ClaudeTranslator()
    emits = translator.feed(
        ResultMessage(
            subtype="success",
            duration_ms=1200,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-2",
            total_cost_usd=0.12,
            usage={"input_tokens": 10, "output_tokens": 5},
        )
    )
    assert emits[0].kind == "turn_completed"
    assert emits[0].fields["stop_reason"] == "completed"
    assert emits[0].fields["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cost_usd": 0.12,
    }
    assert translator.session_id == "sess-2"


def test_an_interrupted_turn_reports_interrupted() -> None:
    translator = ClaudeTranslator()
    translator.mark_interrupted()
    emits = translator.feed(
        ResultMessage(
            subtype="error_during_execution",
            duration_ms=10,
            duration_api_ms=5,
            is_error=True,
            num_turns=1,
            session_id="s",
        )
    )
    completed = next(emit for emit in emits if emit.kind == "turn_completed")
    assert completed.fields["stop_reason"] == "interrupted"


def test_tool_kind_and_title_cover_the_common_tools() -> None:
    assert tool_kind("Grep") == "search"
    assert tool_kind("mcp__ctx__search") == "mcp"
    assert tool_kind("Unknown") == "other"
    assert tool_title("Read", {"file_path": "/repo/x.py"}, "/repo") == "x.py"
    assert tool_title("WebSearch", {"query": "asyncio"}) == "asyncio"
    assert tool_title("Task", {"description": "audit"}) == "audit"


def test_result_text_flattens_content_lists() -> None:
    assert result_text([{"type": "text", "text": "a"}, {"type": "image"}]) == "a\n[image]"
    assert result_text("plain") == "plain"
    assert result_text(None) == ""
