"""Amendment A30: words another agent put into a Claude conversation.

The rows below are the shapes Claude Code really writes, copied from a
transcript on a developer machine and emptied of anything private.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import ToolResultBlock, UserMessage

from rc_client.agents.claude import transcripts
from rc_client.agents.claude.injected import classify, strip_reminders
from rc_client.agents.claude.transcripts import TranscriptTailer
from rc_client.agents.claude.translate import ClaudeTranslator

TEAMMATE_TEXT = (
    "Another Claude session sent a message:\n"
    '<teammate-message teammate_id="recon-ios" color="green">\n'
    '{"type":"idle_notification","from":"recon-ios",'
    '"timestamp":"2026-09-14T12:50:03.588Z","idleReason":"available",'
    '"result":"Recon complete. Fact sheet written to the scratchpad; '
    'three findings need a decision."}\n'
    "</teammate-message>"
)
TEAMMATE_SAID = (
    "recon-ios: Recon complete. Fact sheet written to the scratchpad; "
    "three findings need a decision."
)

TASK_TEXT = (
    "<task-notification>\n"
    "<task-id>a0fd46b27d59a1ab8</task-id>\n"
    "<tool-use-id>toolu_01AQeQCefP73dsY5mSzVHtqC</tool-use-id>\n"
    "<output-file>/tmp/tasks/a0fd46b27d59a1ab8.output</output-file>\n"
    "<status>completed</status>\n"
    '<summary>Agent "How Claude Code titles sessions" finished</summary>\n'
    "<note>A task-notification fires each time this agent stops.</note>\n"
    "<result>The CLI writes the title into the transcript as a row of its own."
    "</result>\n"
    "</task-notification>\n"
    "<system-reminder>\n"
    "Goal check-in: the goal is still active and evaluation was deferred.\n"
    "</system-reminder>"
)
TASK_SAID = (
    'Agent "How Claude Code titles sessions" finished\n'
    "The CLI writes the title into the transcript as a row of its own."
)

PROMPT_TEXT = (
    "Have a look at the screenshot and tell me what broke the layout.\n"
    "<system-reminder>\nThe user opened the file settings.tsx.\n</system-reminder>"
)
PROMPT_SAID = "Have a look at the screenshot and tell me what broke the layout."

REMINDER_ONLY = "<system-reminder>\nBackground work is still running.\n</system-reminder>"

CHANNEL_TEXT = '<channel source="rc" origin="app" message_id="m1">\nhello\n</channel>'


def teammate_row(text: str = TEAMMATE_TEXT) -> dict[str, Any]:
    """A teammate's report: no `origin`, no `promptSource`, only the envelope."""
    return {
        "parentUuid": "c47f36ec-1922-4599-af04-0e033638f5a1",
        "isSidechain": False,
        "promptId": "c07431bc-cb7a-4502-86bd-720b68851562",
        "type": "user",
        "uuid": "row-teammate",
        "timestamp": "2026-09-14T12:50:04.126Z",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": "/repo",
        "sessionId": "sess-1",
        "version": "2.1.266",
        "message": {"role": "user", "content": text},
    }


def task_row(text: str = TASK_TEXT) -> dict[str, Any]:
    """A background task's notification, which the CLI does attribute."""
    return {
        "parentUuid": "13289502-7a86-422a-814b-87fb560a3171",
        "isSidechain": False,
        "type": "user",
        "uuid": "row-task",
        "timestamp": "2026-09-14T13:14:17.876Z",
        "permissionMode": "bypassPermissions",
        "origin": {"kind": "task-notification"},
        "promptSource": "system",
        "queuePriority": "later",
        "queueOrigin": {"kind": "task-notification", "source": "goal-checkin"},
        "queueSkipAttachments": True,
        "userType": "external",
        "entrypoint": "cli",
        "cwd": "/repo",
        "sessionId": "sess-1",
        "version": "2.1.266",
        "message": {"role": "user", "content": text},
    }


def typed_row(text: str = PROMPT_TEXT) -> dict[str, Any]:
    """What the person typed, as a recent CLI attributes it."""
    return {
        "type": "user",
        "uuid": "row-typed",
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "userType": "external",
        "cwd": "/repo",
        "sessionId": "sess-1",
        "message": {"role": "user", "content": text},
    }


def tailer() -> TranscriptTailer:
    return TranscriptTailer(path="/nonexistent", cwd="/repo")


# ------------------------------------------------------------- the classifier


def test_a_teammate_envelope_reduces_to_who_reported_and_what_they_said() -> None:
    said = classify(teammate_row(), TEAMMATE_TEXT)
    assert said is not None
    assert said.by_agent is True
    assert said.text == TEAMMATE_SAID


def test_a_teammate_report_without_a_result_falls_back_in_order() -> None:
    text = TEAMMATE_TEXT.replace('"result"', '"summary"')
    said = classify(teammate_row(text), text)
    assert said is not None
    assert said.text == TEAMMATE_SAID


def test_a_teammate_envelope_that_is_not_json_keeps_its_body() -> None:
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="web-polish">\nthe screenshots are in\n'
        "</teammate-message>"
    )
    said = classify(teammate_row(text), text)
    assert said is not None
    assert said.by_agent is True
    assert said.text == "the screenshots are in"


def test_a_task_notification_reduces_to_its_summary_and_result() -> None:
    said = classify(task_row(), TASK_TEXT)
    assert said is not None
    assert said.by_agent is True
    assert said.text == TASK_SAID


def test_a_notification_with_nothing_to_report_produces_no_block() -> None:
    text = "<task-notification>\n<task-id>b7gmyqaui</task-id>\n</task-notification>"
    assert classify(task_row(text), text) is None


def test_a_person_prompt_keeps_its_words_without_the_reminder() -> None:
    said = classify(typed_row(), PROMPT_TEXT)
    assert said is not None
    assert said.by_agent is False
    assert said.text == PROMPT_SAID


def test_a_row_that_held_only_reminders_produces_no_block() -> None:
    assert classify(typed_row(REMINDER_ONLY), REMINDER_ONLY) is None


def test_every_reminder_comes_off_wherever_it_sits() -> None:
    text = f"{REMINDER_ONLY}\nkeep this\n{REMINDER_ONLY}\nand this\n{REMINDER_ONLY}"
    assert strip_reminders(text) == "keep this\n\nand this"


def test_an_origin_the_cli_does_not_call_human_is_another_agent() -> None:
    row = {"type": "user", "origin": {"kind": "coordinator"}, "uuid": "row-c"}
    said = classify(row, "the goal was reached")
    assert said is not None
    assert said.by_agent is True
    assert said.text == "the goal was reached"


def test_a_channel_injection_is_this_device_not_another_agent() -> None:
    row = {"type": "user", "origin": {"kind": "channel", "server": "rc"}, "promptSource": "system"}
    said = classify(row, "hello from the phone")
    assert said is not None
    assert said.by_agent is False


# ---------------------------------------------------------------- transcripts


def test_a_teammate_message_is_published_as_an_agent_message() -> None:
    reader = tailer()
    emits = reader.translate(teammate_row())
    assert [emit.kind for emit in emits] == ["user_message"]
    assert emits[0].fields == {
        "block_id": "row-teammate",
        "text": TEAMMATE_SAID,
        "source": "agent",
    }
    assert reader.awaiting_reply is True
    assert reader.turn_trigger == "agent"


def test_a_task_notification_is_published_as_an_agent_message() -> None:
    reader = tailer()
    emits = reader.translate(task_row())
    assert [emit.kind for emit in emits] == ["user_message"]
    assert emits[0].fields["source"] == "agent"
    assert emits[0].fields["text"] == TASK_SAID
    assert reader.turn_trigger == "agent"


def test_a_typed_prompt_stays_terminal_with_the_reminder_gone() -> None:
    reader = tailer()
    emits = reader.translate(typed_row())
    assert emits[0].fields == {
        "block_id": "row-typed",
        "text": PROMPT_SAID,
        "source": "terminal",
    }
    assert reader.turn_trigger == "terminal"


def test_a_reminder_only_row_starts_no_turn() -> None:
    reader = tailer()
    assert reader.translate(typed_row(REMINDER_ONLY)) == []
    assert reader.awaiting_reply is False
    assert reader.turn_trigger == "terminal"


def test_the_echo_of_our_own_injection_is_still_neither() -> None:
    reader = tailer()
    row = {
        "type": "user",
        "uuid": "row-echo",
        "isMeta": True,
        "origin": {"kind": "channel", "server": "rc"},
        "message": {"role": "user", "content": CHANNEL_TEXT},
    }
    emits = reader.translate(row)
    assert [emit.kind for emit in emits] == [transcripts.CHANNEL_DELIVERED]
    assert reader.turn_trigger == "remote"


def test_an_agent_message_arriving_in_content_blocks_is_read_the_same_way() -> None:
    reader = tailer()
    row = dict(teammate_row(), uuid="row-blocks")
    row["message"] = {"role": "user", "content": [{"type": "text", "text": TEAMMATE_TEXT}]}
    emits = reader.translate(row)
    assert emits[0].fields["source"] == "agent"
    assert emits[0].fields["text"] == TEAMMATE_SAID


# ------------------------------------------------------------------ the SDK


def test_an_injected_row_reaches_a_driven_session_as_an_agent_message() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    emits = translator.feed(UserMessage(content=TEAMMATE_TEXT, uuid="sdk-1"))
    assert [emit.kind for emit in emits] == ["user_message"]
    assert emits[0].fields == {
        "block_id": "sdk-1",
        "text": TEAMMATE_SAID,
        "source": "agent",
    }


def test_a_task_notification_the_sdk_attributes_is_an_agent_message() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    message = UserMessage(
        content="the background build finished",
        uuid="sdk-2",
        origin={"kind": "task-notification"},
    )
    emits = translator.feed(message)
    assert emits[0].fields["source"] == "agent"
    assert emits[0].fields["text"] == "the background build finished"


def test_a_prompt_this_device_sent_is_never_doubled() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    assert translator.feed(UserMessage(content="ship it", uuid="sdk-3")) == []
    typed = UserMessage(content="ship it", uuid="sdk-4", origin={"kind": "human"})
    assert translator.feed(typed) == []


def test_tool_results_still_arrive_as_tool_rows() -> None:
    translator = ClaudeTranslator(cwd="/repo")
    block = ToolResultBlock(tool_use_id="toolu_1", content="a\nb")
    emits = translator.feed(UserMessage(content=[block], uuid="sdk-5"))
    assert [emit.kind for emit in emits] == ["tool_call"]
    assert emits[0].fields["output"] == "a\nb"
