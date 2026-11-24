"""`agy`'s updates as protocol events, its options and its command list (A64)."""

from __future__ import annotations

from typing import Any

import pytest

from rc_client.agents.antigravity import commands, options, translate
from rc_client.agents.antigravity.translate import Translator

WRITE = {
    "sessionUpdate": "tool_call",
    "toolCallId": "call_18348425608940957344",
    "name": "write_file",
    "kind": "edit",
    "status": "in_progress",
    "title": "File probe.txt",
    "locations": [{"path": "/work/probe.txt"}],
    "rawInput": {"Content": "hello from probe\n", "Path": "/work/probe.txt"},
    "content": [{"type": "diff", "path": "/work/probe.txt", "newText": "hello from probe\n"}],
}
READ_OUTSIDE = {
    "toolCallId": "call_1",
    "name": "read_file",
    "kind": "read",
    "status": "pending",
    "title": "File ../attach/note.txt",
    "rawInput": {"Path": "/elsewhere/attach/note.txt"},
}


def test_a_file_tool_is_titled_by_its_absolute_path_not_agy_s_relative_title() -> None:
    tool = translate.new_tool(WRITE)
    emit = tool.emit()
    assert (emit.fields["tool"], emit.fields["tool_kind"], emit.fields["title"]) == (
        "write_file",
        "write",
        "/work/probe.txt",
    )
    assert emit.fields["diff"]["additions"] == 1 and emit.fields["diff"]["deletions"] == 0
    assert translate.new_tool(READ_OUTSIDE).emit().fields["title"] == "/elsewhere/attach/note.txt"


def test_a_command_is_titled_by_its_command_line_and_reports_its_exit() -> None:
    update = {
        "toolCallId": "call_2",
        "name": "run_command",
        "kind": "execute",
        "status": "completed",
        "title": "ls -la",
        "rawInput": {"CommandLine": "ls -la\necho two", "IsDaemon": False},
        "rawOutput": {"output": "total 0", "exit_code": 0},
    }
    emit = translate.new_tool(update).emit(ended_at=1000)
    assert (emit.fields["title"], emit.fields["status"], emit.fields["summary"]) == (
        "ls -la",
        "succeeded",
        "exit 0",
    )
    assert emit.fields["output"] == "total 0" and emit.fields["ended_at"] == 1000


def test_an_mcp_tool_is_said_so() -> None:
    update = {
        "toolCallId": "c",
        "name": "lookup",
        "kind": "other",
        "_meta": {"is_mcp_tool_call": True},
    }
    assert translate.new_tool(update).tool_kind == "mcp"


@pytest.mark.parametrize(
    ("reason", "stop"),
    [
        ("end_turn", "completed"),
        ("cancelled", "interrupted"),
        ("refusal", "error"),
        ("max_tokens", "error"),
    ],
)
def test_stop_reasons(reason: str, stop: str) -> None:
    assert translate.stop_reason(reason) == stop


def test_text_runs_until_a_tool_begins_and_each_tool_is_its_own_block() -> None:
    translator = Translator()
    first, opened = translator.text("assistant_text", "It ")
    assert opened and first[0].delta
    more, opened_again = translator.text("assistant_text", "printed")
    assert not opened_again and more[0].fields["block_id"] == first[0].fields["block_id"]
    emits, fresh = translator.tool({**WRITE})
    assert fresh
    assert emits[0].fields == {
        "block_id": first[0].fields["block_id"],
        "text": "It printed",
        "done": True,
    }, "the text before the tool is finished first"
    _, fresh_again = translator.tool({**WRITE, "sessionUpdate": "tool_call_update"})
    assert not fresh_again
    after, opened = translator.text("assistant_text", "Done.")
    assert opened and after[0].fields["block_id"] != first[0].fields["block_id"]


def test_a_reply_after_a_tool_finished_is_a_new_block() -> None:
    """agy 1.5.15: a background command's result, and the reply to it, come inside the prompt."""
    translator = Translator()
    started = {**WRITE, "toolCallId": "bg", "name": "run_command", "kind": "execute"}
    translator.tool(started)
    first, _ = translator.text("assistant_text", "I started it in the background.")
    emits, _ = translator.tool(
        {**started, "sessionUpdate": "tool_call_update", "status": "completed"}
    )
    assert [emit.kind for emit in emits] == ["tool_call"], "the words wait for what comes next"
    after, opened = translator.text("assistant_text", "\n\nThe command printed `slept`.")
    assert after[0].fields == {
        "block_id": first[0].fields["block_id"],
        "text": "I started it in the background.",
        "done": True,
    }, "the words before the result are finished first"
    assert opened and after[1].fields["block_id"] != first[0].fields["block_id"]


def test_a_thought_between_a_result_and_the_reply_leaves_the_reply_its_own_block() -> None:
    translator = Translator()
    started = {**WRITE, "toolCallId": "bg", "name": "run_command", "kind": "execute"}
    translator.tool(started)
    first, _ = translator.text("assistant_text", "I started it in the background.")
    translator.tool({**started, "sessionUpdate": "tool_call_update", "status": "completed"})
    translator.text("thinking", "It printed slept.")
    reply, opened = translator.text("assistant_text", "\n\nThe command printed `slept`.")
    assert reply[0].fields["block_id"] == first[0].fields["block_id"] and reply[0].fields["done"]
    assert opened and reply[-1].fields["block_id"] != first[0].fields["block_id"]


def test_words_still_being_written_as_a_tool_finished_stay_one_block() -> None:
    """A sub-agent's tool, or a quick command, can finish while `agy` is mid-sentence."""
    translator = Translator()
    started = {**WRITE, "toolCallId": "quick", "name": "run_command", "kind": "execute"}
    translator.tool(started)
    first, _ = translator.text("assistant_text", "Listing the files")
    translator.tool({**started, "sessionUpdate": "tool_call_update", "status": "completed"})
    more, opened = translator.text("assistant_text", " in the folder now.")
    assert not opened and more[0].fields["block_id"] == first[0].fields["block_id"]
    later, opened_later = translator.text("assistant_text", "\n\nDone.")
    assert not opened_later, "only the words right after the tool can begin a block"
    assert later[0].fields["block_id"] == first[0].fields["block_id"]


def test_a_background_command_or_a_schedule_lingers_and_a_daemon_does_not() -> None:
    translator = Translator()
    background = {
        "toolCallId": "bg",
        "name": "run_command",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"CommandLine": "sleep 12", "IsDaemon": False, "WaitMsBeforeAsync": 500},
    }
    translator.tool(background)
    assert translator.lingering()
    translator.tool({**background, "status": "completed"})
    assert not translator.lingering()
    translator.tool(
        {
            **background,
            "toolCallId": "srv",
            "rawInput": {"CommandLine": "npm run dev", "IsDaemon": True},
        }
    )
    assert not translator.lingering(), "a daemon does not keep the process"
    translator.tool(
        {"toolCallId": "s", "name": "schedule", "kind": "other", "status": "in_progress"}
    )
    assert translator.lingering()


def offered(
    model: str, levels: tuple[str, ...] | None, effort: str | None = None
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = [
        {
            "id": "model",
            "category": "model",
            "currentValue": model,
            "options": [
                {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
                {"value": model, "name": model},
            ],
        }
    ]
    if levels is not None:
        entries.append(
            {
                "id": "thought_level",
                "category": "thought_level",
                "currentValue": effort,
                "options": [{"value": level, "name": level} for level in levels],
            }
        )
    entries.append(
        {
            "id": "mode",
            "category": "mode",
            "currentValue": "default",
            "options": [{"group": "modes", "options": [{"value": "default", "name": "Default"}]}],
        }
    )
    return {"configOptions": entries}


def test_options_are_read_from_agy_s_own_list() -> None:
    running = options.running(offered("gemini-3.8-flash", ("low", "medium", "high"), "medium"))
    assert running.model == "gemini-3.8-flash"
    assert running.levels == ("low", "medium", "high") and running.effort == "medium"
    assert [choice.id for choice in running.modes] == ["default"] and running.mode_option
    bare = options.running(offered("gpt-oss-120b-medium", None))
    assert bare.levels is None and bare.effort is None


@pytest.mark.parametrize(
    ("effort", "levels", "clamped"),
    [
        ("xhigh", ("low", "medium", "high", "xhigh"), "xhigh"),
        ("xhigh", ("low", "medium", "high"), "high"),
        ("high", ("low", "medium"), "medium"),
        ("low", ("medium", "high"), "medium"),
        ("high", None, None),
        ("high", (), None),
        (None, ("low",), None),
    ],
)
def test_the_level_is_kept_or_the_nearest_below_or_none(
    effort: str | None, levels: tuple[str, ...] | None, clamped: str | None
) -> None:
    assert options.clamp(effort, levels) == clamped


def test_the_command_list_is_agy_s_and_the_device_keeps_the_last_one() -> None:
    update = {
        "availableCommands": [
            {"name": "goal", "description": "Run until\nthe goal is done."},
            {"name": "goal", "description": "twice"},
            {"name": "Bad Name", "description": "refused by 4.11"},
            {"name": "compact", "description": ""},
        ]
    }
    listed = commands.advertised(update)
    assert [(c.name, c.description, c.argument) for c in listed] == [
        ("goal", "Run until the goal is done.", None),
        ("compact", "compact", None),
    ]
    commands.remember(listed)
    assert commands.recall() == listed
    assert commands.typed("goal", "ship it") == "/goal ship it"
