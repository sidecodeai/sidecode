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


# ------------------------------------- A51: rows the CLI hides that start a turn

PEER_TEXT = (
    "Another Claude session sent a message:\n"
    '<agent-message from="kotlin-update">\n'
    "Emulator run finished: both flows pass.\n"
    "</agent-message>\n"
    "Reply with the SendMessage tool if a reply is needed."
)


def peer_row() -> dict[str, Any]:
    """Claude Code's shape for a message a teammate of the session sends: hidden, `peer`."""
    return {
        "type": "user",
        "uuid": "row-peer",
        "isMeta": True,
        "promptSource": "system",
        "origin": {
            "kind": "peer",
            "from": "kotlin-update",
            "name": "kotlin-update",
            "senderTaskId": "akotlin-update-1",
            "body": "Emulator run finished: both flows pass.",
        },
        "message": {"role": "user", "content": PEER_TEXT},
    }


def test_a_hidden_peer_message_starts_a_turn_and_reads_as_an_agent_message() -> None:
    """A device that missed this turn injected into it, and the CLI absorbed the message."""
    reader = tailer()
    emits = reader.translate(peer_row())
    assert reader.awaiting_reply is True
    assert reader.turn_trigger == "agent"
    assert [emit.kind for emit in emits] == ["user_message"]
    assert emits[0].fields == {
        "block_id": "row-peer",
        "text": "kotlin-update: Emulator run finished: both flows pass.",
        "source": "agent",
    }


def test_another_sessions_cross_session_envelope_reads_the_same_way() -> None:
    """Another session's envelope names the sender in `from-name`; `from` is a socket."""
    reader = tailer()
    row = {
        **peer_row(),
        "origin": {"kind": "peer", "from": "uds:/tmp/cc-socks/41.sock", "name": "work2-10"},
        "message": {
            "role": "user",
            "content": (
                "Another Claude session sent a message:\n"
                '<cross-session-message from="uds:/tmp/cc-socks/41.sock" from-name="work2-10" '
                'from-mode="bypass">\n'
                "Emulator run finished: both flows pass.\n"
                "</cross-session-message>\n\n"
                "This came from another Claude session — not typed by your user."
            ),
        },
    }
    emits = reader.translate(row)
    assert reader.turn_trigger == "agent"
    assert emits[0].fields["text"] == "work2-10: Emulator run finished: both flows pass."
    assert emits[0].fields["source"] == "agent"


def test_a_continuation_the_cli_starts_itself_is_a_turn_without_a_block() -> None:
    reader = tailer()
    row = {
        "type": "user",
        "uuid": "row-continue",
        "isMeta": True,
        "promptSource": "system",
        "origin": {"kind": "auto-continuation"},
        "message": {"role": "user", "content": "Your usage limit has reset. Continue the task."},
    }
    assert reader.translate(row) == []
    assert (reader.awaiting_reply, reader.turn_trigger) == (True, "agent")


def fold(prompt: Any, **extra: Any) -> dict[str, Any]:
    """A message Claude Code folded into the running turn at a tool boundary."""
    attachment = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt", **extra}
    return {"type": "attachment", "uuid": "row-fold", "attachment": attachment}


def test_a_prompt_the_person_queued_mid_turn_is_published_where_it_was_folded_in() -> None:
    reader = tailer()
    emits = reader.translate(fold(PROMPT_TEXT, origin={"kind": "human"}))
    assert [emit.kind for emit in emits] == ["user_message"]
    assert emits[0].fields == {"block_id": "row-fold", "text": PROMPT_SAID, "source": "terminal"}
    assert reader.awaiting_reply is False, "it joins a turn and starts none"


def test_folded_words_of_another_agent_read_as_they_do_when_they_start_a_turn() -> None:
    peer = fold(PEER_TEXT, isMeta=True, origin={"kind": "peer", "from": "kotlin-update"})
    notice = fold(TASK_TEXT, commandMode="task-notification", origin={"kind": "task-notification"})
    for row, said in (
        (peer, "kotlin-update: Emulator run finished: both flows pass."),
        (notice, TASK_SAID),
    ):
        [emit] = tailer().translate(row)
        assert (emit.fields["text"], emit.fields["source"]) == (said, "agent")


def test_folded_rows_that_would_show_nothing_as_a_turn_show_nothing() -> None:
    for row in (
        fold("Your usage limit has reset.", isMeta=True, origin={"kind": "auto-continuation"}),
        fold(REMINDER_ONLY, origin={"kind": "human"}),
        fold("ls -la", commandMode="bash", origin={"kind": "human"}),
        fold('{"event": "ci.finished"}', commandMode="poll-event"),
    ):
        assert tailer().translate(row) == []


def test_our_own_folded_injection_is_an_absorption_and_never_a_bubble() -> None:
    row = fold(CHANNEL_TEXT, isMeta=True, origin={"kind": "channel", "server": "rc"})
    emits = tailer().translate(row)
    assert [(emit.kind, emit.fields) for emit in emits] == [
        (transcripts.CHANNEL_ABSORBED, {"message_id": "m1"})
    ]


def command_row(name: str) -> dict[str, Any]:
    """The row a slash command typed at the terminal leaves."""
    return {
        "type": "user",
        "uuid": f"row-{name}",
        "origin": {"kind": "human"},
        "message": {
            "role": "user",
            "content": (
                f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
            ),
        },
    }


def test_what_a_command_expands_to_starts_its_turn() -> None:
    """A skill or a command of the person's is a turn, hidden behind its own row (A51)."""
    reader = tailer()
    assert reader.translate(command_row("e2e-skill")) == []
    assert reader.awaiting_reply is False
    expansion = {
        "type": "user",
        "uuid": "row-expansion",
        "isMeta": True,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Base directory: x\n\nwork"}],
        },
    }
    assert reader.translate(expansion) == []
    assert (reader.awaiting_reply, reader.turn_trigger) == (True, "terminal")


def test_a_command_that_only_answers_starts_nothing() -> None:
    reader = tailer()
    reader.translate(command_row("model"))
    output = {
        "type": "user",
        "uuid": "row-out",
        "message": {
            "role": "user",
            "content": "<local-command-stdout>Set model to haiku</local-command-stdout>",
        },
    }
    reader.translate(output)
    later = {"type": "user", "isMeta": True, "message": {"content": "[Image: original 10x10]"}}
    assert reader.translate(later) == []
    assert reader.awaiting_reply is False


def test_somebodys_stop_hook_sends_the_ending_turn_on() -> None:
    reader = tailer()
    reader.translate({"type": "user", "uuid": "p", "message": {"role": "user", "content": "go"}})
    reader.translate(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
            },
        }
    )
    assert reader.awaiting_reply is False
    feedback = {
        "type": "user",
        "isMeta": True,
        "message": {"role": "user", "content": "Stop hook feedback:\nrun the tests too"},
    }
    assert reader.translate(feedback) == []
    assert (reader.awaiting_reply, reader.turn_trigger) == (True, "terminal")


def test_every_channel_message_a_row_holds_is_confirmed() -> None:
    reader = tailer()
    text = "\n".join(
        f'<channel source="rc" origin="app" message_id="{name}">\n{name}\n</channel>'
        for name in ("m1", "m2")
    )
    row = {
        "type": "user",
        "isMeta": True,
        "origin": {"kind": "channel", "server": "rc"},
        "message": {"role": "user", "content": text},
    }
    emits = reader.translate(row)
    assert [(emit.kind, emit.fields) for emit in emits] == [
        (transcripts.CHANNEL_DELIVERED, {"message_id": "m1"}),
        (transcripts.CHANNEL_DELIVERED, {"message_id": "m2"}),
    ]


def test_a_tool_call_proves_a_turn_the_rows_never_showed_starting() -> None:
    reader = tailer()
    row = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
            ],
            "stop_reason": "tool_use",
        },
    }
    reader.translate(row)
    assert reader.awaiting_reply is True


def test_hidden_rows_that_belong_to_a_prompt_start_nothing() -> None:
    reader = tailer()
    for row in (
        {"type": "user", "isMeta": True, "message": {"content": "[Image: original 2560x1600]"}},
        {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>x"}},
        {"type": "user", "isMeta": True, "message": {"content": REMINDER_ONLY}},
    ):
        assert reader.translate(row) == []
    assert reader.awaiting_reply is False


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
