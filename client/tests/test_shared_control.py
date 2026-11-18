"""Amendment A10: a session the device shares with a live terminal CLI."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rc_client import attachments
from rc_client.agents.claude import transcripts
from rc_client.errors import RcError
from rc_client.ids import block_uuid
from rc_client.models import AgentInfo, Choice
from rc_client.registry import Registry
from rc_client.sessions import limit_dialog
from rc_client.sessions.attach import Attachment, HookQuestion, TurnHook
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.limits import LimitStop, TurnEnd
from rc_client.sessions.shared import SharedState
from tests.helpers import drained, event_validator, tick
from tests.limit_helpers import (
    CONVERSATION,
    CREDITS,
    PROMPT_BOX,
    SAID,
    STOP_WAIT,
    WAIT_AT,
    WAIT_SHORTLY,
    Clock,
    LimitTerminal,
    menu_text,
    quick,
    until,
    write_record,
)

QUESTION_TOOL = "AskUserQuestion"
QUESTION_INPUT = {
    "questions": [
        {
            "header": "Skew",
            "question": "How should the refresh window treat skew?",
            "options": [{"label": "Clamp it"}, {"label": "Go monotonic"}],
            "multiSelect": False,
        }
    ]
}
PROMPT = "How should the refresh window treat skew?"

# Amendment A30: what one Claude session sends another, as the CLI files it.
TEAMMATE_MESSAGE = (
    "Another Claude session sent a message:\n"
    '<teammate-message teammate_id="web-polish" color="green">\n'
    '{"type":"idle_notification","from":"web-polish","idleReason":"available",'
    '"result":"Screenshots are in the scratchpad."}\n'
    "</teammate-message>"
)


class FakeHookQuestion(HookQuestion):
    """A question hook that records the single reply it is given."""

    def __init__(self, session_id: str = "sess-1") -> None:
        super().__init__(
            session_id=session_id, cwd="/repo", tool=QUESTION_TOOL, input=dict(QUESTION_INPUT)
        )
        self.replies: list[dict[str, Any] | None] = []

    async def answer(self, answers: dict[str, Any] | None) -> bool:
        self.replies.append(answers)
        return True


class FakeAttachment(Attachment):
    """An attachment that records what it would have sent to the bridge."""

    def __init__(self, session_id: str = "sess-1", cwd: str = "/repo") -> None:
        super().__init__(session_id=session_id, cwd=cwd, pid=4242, claude_version="2.1.267")
        self.injected: list[tuple[str, str]] = []
        self.verdicts: list[tuple[str, str]] = []
        self.reachable = True

    async def inject(self, message_id: str, text: str) -> bool:
        if not self.reachable:
            return False
        self.injected.append((message_id, text))
        return True

    async def relay_permission(self, request_id: str, behavior: str) -> bool:
        self.verdicts.append((request_id, behavior))
        return True

    def detach(self) -> None:
        self.reachable = False


def assistant_row(content: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    """One content block of an assistant message, as the CLI files it."""
    return {
        "type": "assistant",
        "uuid": "a1",
        "message": {"id": "msg_1", "content": content, "stop_reason": stop_reason},
    }


def agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["takeover", "interrupt", "queue", "attachments", "history"],
            attach="channel",
            attach_ready=True,
            shared_interrupt=False,
            shared_attachments=True,
        )
    ]


class Harness:
    """A hub with one attached session and the frames it published."""

    def __init__(self, tmp_path: Path) -> None:
        self.frames: list[dict[str, Any]] = []
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", agents)
        self.attachment = FakeAttachment()

    async def attach(self) -> SessionEntry:
        await self.hub.attach_registered(self.attachment)
        return self.hub.entry("sess-1")

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def sessions(self) -> list[dict[str, Any]]:
        return [frame["session"] for frame in self.frames if frame.get("type") == "session.updated"]

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


async def test_registering_creates_a_shared_session_and_announces_the_control_change(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    assert entry.session.control == "shared"
    assert entry.session.origin == "terminal"
    assert entry.session.cwd == "/repo"
    assert entry.session.state == "idle"
    assert harness.events("meta")[0]["control"] == "shared"
    assert harness.sessions()[-1]["control"] == "shared"


async def test_a_shared_session_is_never_readonly(harness: Harness) -> None:
    entry = await harness.attach()
    states = [entry.session.state]
    await tick(harness.hub, entry, running=True)
    states.append(entry.session.state)
    await tick(harness.hub, entry, running=False)
    states.append(entry.session.state)
    assert states == ["idle", "running", "idle"]
    assert "readonly" not in states


async def test_sending_while_idle_injects_at_once_and_shows_one_delivered_bubble(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"})

    assert result == {"accepted": "sent"}
    assert [text for _, text in harness.attachment.injected] == ["go"]
    messages = harness.events("user_message")
    assert len(messages) == 1
    assert messages[0]["delivery"] == "delivered"
    assert messages[0]["source"] == "remote"
    assert entry.queue == []
    assert entry.session.title == "go"


async def test_sending_mid_turn_holds_the_message_until_the_transcript_goes_idle(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)

    result = await harness.hub.send({"id": "req-2", "session_id": "sess-1", "text": "later"})
    assert result == {"accepted": "queued", "queued_id": "req-2"}
    assert harness.attachment.injected == []
    # Amendment A19: a held message is a queue entry and nothing more.
    assert harness.events("user_message") == []
    assert harness.events("queue")[-1]["pending"] == [
        {"id": "req-2", "text": "later", "ts": entry.queue[0]["ts"]}
    ]

    await tick(harness.hub, entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["later"]
    bubbles = harness.events("user_message")
    assert len(bubbles) == 1
    assert bubbles[-1]["delivery"] == "delivered"
    assert harness.events("queue")[-1]["pending"] == []


async def test_only_one_message_is_in_flight_until_the_transcript_confirms_it(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.send({"id": "a", "session_id": "sess-1", "text": "one"})
    await harness.hub.send({"id": "b", "session_id": "sess-1", "text": "two"})

    assert [text for _, text in harness.attachment.injected] == ["one"]
    assert [item["text"] for item in entry.queue] == ["two"]

    # The transcript has not caught up yet, so a tick must not release the second.
    await tick(harness.hub, entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["one"]

    message_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_delivered(entry, message_id)
    await tick(harness.hub, entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["one", "two"]


async def test_the_transcript_echo_of_an_injection_is_not_a_second_bubble() -> None:
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    row = {
        "type": "user",
        "uuid": "row-1",
        "isMeta": True,
        "origin": {"kind": "channel", "server": "rc"},
        "message": {
            "role": "user",
            "content": '<channel source="rc" origin="app" message_id="m1">\nhello\n</channel>',
        },
    }
    emits = tailer.translate(row)
    assert [emit.kind for emit in emits] == [transcripts.CHANNEL_DELIVERED]
    assert emits[0].fields == {"message_id": "m1"}
    assert tailer.awaiting_reply is True


async def test_a_message_typed_in_the_terminal_still_becomes_a_bubble() -> None:
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    emits = tailer.translate(
        {
            "type": "user",
            "uuid": "row-2",
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": "typed here"},
        }
    )
    assert [emit.kind for emit in emits] == ["user_message"]
    assert emits[0].fields["source"] == "terminal"


async def test_an_absorbed_injection_goes_in_again_three_times_and_then_fails_visibly(
    harness: Harness,
) -> None:
    """A51: each turn's end sends it again until its own row shows, at most three times.

    Absorbed after the third re-send, it is an `error` the apps show in the
    bubble's place, under its block id: nothing delivered it.
    """
    entry = await harness.attach()
    await harness.hub.send({"id": "req-3", "session_id": "sess-1", "text": "do it"})
    first = harness.events("user_message")[0]

    for attempt in range(1, 4):
        message_id, _ = harness.attachment.injected[-1]
        await harness.hub.shared.echo_absorbed(entry, message_id)
        bubble = harness.events("user_message")[-1]
        assert (bubble["delivery"], bubble["block_id"]) == ("absorbed", first["block_id"])
        assert [item["text"] for item in entry.queue] == ["do it"]
        await tick(harness.hub, entry, running=False)
        assert [text for _, text in harness.attachment.injected] == ["do it"] * (attempt + 1)
        assert harness.events("user_message")[-1]["delivery"] == "delivered"
    assert harness.events("error") == []

    last_id, _ = harness.attachment.injected[-1]
    await harness.hub.shared.echo_absorbed(entry, last_id)
    assert entry.queue == []
    [error] = harness.events("error")
    assert error["message"] == (
        'The terminal did not take "do it" as a message after 4 tries; send it again.'
    )
    assert error["block_id"] == first["block_id"] == block_uuid("req-3")
    assert harness.events("user_message")[-1]["delivery"] == "absorbed"
    await tick(harness.hub, entry, running=False)
    assert len(harness.attachment.injected) == 4


async def test_the_absorbed_attachment_row_is_recognised_by_its_message_id() -> None:
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    emits = tailer.translate(
        {
            "type": "attachment",
            "attachment": {
                "type": "queued_command",
                "prompt": '<channel source="rc" origin="app" message_id="q1">\nhi\n</channel>',
                "origin": {"kind": "channel", "server": "rc"},
                "isMeta": True,
            },
        }
    )
    assert [emit.kind for emit in emits] == [transcripts.CHANNEL_ABSORBED]
    assert emits[0].fields == {"message_id": "q1"}


async def test_an_approval_is_relayed_with_only_allow_and_deny(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.attach_permission_request(
        harness.attachment,
        {
            "request_id": "rttfm",
            "tool_name": "Write",
            "description": "Write a file to the local filesystem.",
            "input_preview": '{"file_path": "/repo/hello.txt", "content": "hi"}',
        },
    )
    approval = harness.events("approval")[-1]
    assert approval["status"] == "pending"
    assert [option["id"] for option in approval["options"]] == ["allow", "deny"]
    assert approval["tool_kind"] == "write"
    assert approval["title"] == "hello.txt"
    assert approval["input"]["tool_name"] == "Write"
    assert "diff" not in approval
    assert entry.session.state == "needs_approval"

    await harness.hub.approve(
        {"session_id": "sess-1", "request_id": approval["request_id"], "option_id": "allow"}
    )
    assert harness.attachment.verdicts == [("rttfm", "allow")]
    resolved = harness.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "allow", "by": "remote"}
    assert resolved["block_id"] == approval["block_id"]
    assert entry.session.state != "needs_approval"


async def test_replying_to_an_approval_the_terminal_already_answered_is_a_no_op(
    harness: Harness,
) -> None:
    await harness.attach()
    assert (
        await harness.hub.approve(
            {"session_id": "sess-1", "request_id": "gone", "option_id": "deny"}
        )
        == {}
    )
    assert harness.attachment.verdicts == []
    with pytest.raises(RcError) as raised:
        await harness.hub.approve(
            {"session_id": "sess-1", "request_id": "gone", "option_id": "allow_session"}
        )
    assert raised.value.code == "bad_request"


async def test_an_approval_answered_in_the_terminal_is_closed_from_the_transcript(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r1", "tool_name": "Bash", "description": "run", "input_preview": "ls"},
    )
    await harness.hub.shared.tool_finished(entry, "Bash", failed=False)

    resolved = harness.events("approval")[-1]
    assert resolved["decision"] == {"option_id": "allow", "by": "terminal"}
    assert harness.attachment.verdicts == [], "the terminal already answered"

    state = entry.shared
    assert isinstance(state, SharedState)
    assert state.approvals == {}


async def test_a_refused_tool_closes_its_approval_as_denied_in_the_terminal(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r2", "tool_name": "Write", "description": "w", "input_preview": "{}"},
    )
    await harness.hub.shared.tool_finished(entry, "Write", failed=True)
    assert harness.events("approval")[-1]["decision"]["option_id"] == "deny"


async def test_closing_the_bridge_expires_approvals_and_hands_control_back(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r3", "tool_name": "Bash", "description": "d", "input_preview": "ls"},
    )
    await harness.hub.shared.closed(entry, harness.attachment, "none")

    assert entry.shared is None
    assert entry.session.control == "none"
    assert entry.session.state != "readonly"
    assert harness.events("approval")[-1]["status"] == "expired"
    assert harness.events("meta")[-1]["control"] == "none"


async def test_closing_the_bridge_with_a_live_cli_leaves_the_session_to_the_terminal(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.shared.closed(entry, harness.attachment, "terminal")
    assert entry.session.control == "terminal"
    assert entry.session.state == "readonly"


async def test_messages_still_pending_survive_the_detachment(harness: Harness) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send({"id": "req-4", "session_id": "sess-1", "text": "afterwards"})
    await harness.hub.shared.closed(entry, harness.attachment, "none")

    assert [item["text"] for item in entry.queue] == ["afterwards"]
    assert harness.events("queue")[-1]["pending"][0]["id"] == "req-4"


async def test_the_requests_a_shared_session_refuses(harness: Harness) -> None:
    await harness.attach()
    with pytest.raises(RcError) as raised:
        await harness.hub.stop({"session_id": "sess-1"})
    assert raised.value.code == "unsupported"

    with pytest.raises(RcError) as raised:
        await harness.hub.set_options({"session_id": "sess-1", "model": "opus"})
    assert raised.value.code == "unsupported"

    with pytest.raises(RcError) as raised:
        await harness.hub.takeover({"session_id": "sess-1"})
    assert raised.value.code == "conflict"
    assert raised.value.message == "already attached"


async def test_a_pending_message_can_be_removed_from_the_queue(harness: Harness) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send({"id": "req-5", "session_id": "sess-1", "text": "never mind"})
    await harness.hub.queue_remove({"session_id": "sess-1", "queued_id": "req-5"})
    await tick(harness.hub, entry, running=False)
    assert harness.attachment.injected == []


async def test_an_injected_turn_opens_and_closes_a_turn_for_the_apps(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.send({"id": "req-t", "session_id": "sess-1", "text": "go"})

    turns = [entry.session.turn]
    assert entry.session.state == "running"
    started = harness.events("turn_started")[-1]
    assert started["trigger"] == "remote"

    message_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_delivered(entry, message_id)
    await tick(harness.hub, entry, running=False)
    turns.append(entry.session.turn)
    assert [turn is not None for turn in turns] == [True, False]
    assert harness.events("turn_completed")[-1]["stop_reason"] == "completed"


async def test_a_turn_the_terminal_started_is_labelled_as_such(harness: Harness) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    assert harness.events("turn_started")[-1]["trigger"] == "terminal"
    await tick(harness.hub, entry, running=False)
    assert harness.events("turn_completed")[-1]["stop_reason"] == "completed"


async def test_a_turn_another_agent_started_is_labelled_agent(harness: Harness) -> None:
    """Amendment A30: the trigger comes from the rows the transcript just gave up."""
    entry = await harness.attach()
    reader = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    reader.translate(
        {
            "type": "user",
            "uuid": "row-teammate",
            "message": {"role": "user", "content": TEAMMATE_MESSAGE},
        }
    )
    assert reader.turn_trigger == "agent"

    await tick(harness.hub, entry, reader.busy, reader.turn_trigger)
    assert harness.events("turn_started")[-1]["trigger"] == "agent"

    await tick(harness.hub, entry, running=False)
    assert harness.events("turn_completed")[-1]["stop_reason"] == "completed"


async def test_a_turn_the_person_interrupted_is_closed_as_interrupted(harness: Harness) -> None:
    """Amendment A32: the marker ends the turn it found and starts none of its own."""
    entry = await harness.attach()
    reader = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    reader.translate(
        {"type": "user", "uuid": "row-typed", "message": {"role": "user", "content": "go"}}
    )
    await tick(
        harness.hub,
        entry,
        reader.busy,
        reader.turn_trigger,
        TurnEnd(reader.stop_reason, reader.limit),
    )
    assert harness.events("turn_started")[-1]["trigger"] == "terminal"
    starts = len(harness.events("turn_started"))

    reader.translate(
        {
            "type": "user",
            "uuid": "row-stop",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user]"}],
            },
        }
    )
    assert reader.busy is False
    await tick(
        harness.hub,
        entry,
        reader.busy,
        reader.turn_trigger,
        TurnEnd(reader.stop_reason, reader.limit),
    )
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    assert len(harness.events("turn_started")) == starts
    assert entry.session.turn is None


async def test_a_turn_still_open_when_the_bridge_leaves_is_closed_as_stopped(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.shared.closed(entry, harness.attachment, "none")
    assert entry.session.turn is None
    assert harness.events("turn_completed")[-1]["stop_reason"] == "stopped"


async def test_a_control_change_alone_emits_meta_without_a_status_event(
    harness: Harness,
) -> None:
    """A10 clarification 3: `status` follows a transition only when `state` moves."""
    entry = await harness.attach()
    assert harness.events("meta")[-1]["control"] == "shared"
    assert harness.events("status") == [], "the session was already idle"

    before = len(harness.events("status"))
    await harness.hub.shared.closed(entry, harness.attachment, "none")
    assert harness.events("meta")[-1]["control"] == "none"
    assert len(harness.events("status")) == before, "idle to idle moves no state"
    assert harness.sessions()[-1]["control"] == "none"


async def test_a_transition_that_moves_the_state_emits_both(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.shared.closed(entry, harness.attachment, "terminal")
    assert harness.events("meta")[-1]["control"] == "terminal"
    assert harness.events("status")[-1]["state"] == "readonly"
    assert harness.sessions()[-1]["state"] == "readonly"


# -------------------------------------------- A12: the app's id is the bubble

SEND_REQUEST = "c41b90d7-52a8-4e3f-9d16-70ab2ce8f145"


async def test_a_message_injected_into_a_terminal_keeps_the_request_id(
    harness: Harness,
) -> None:
    await harness.attach()
    await harness.hub.send({"id": SEND_REQUEST, "session_id": "sess-1", "text": "go"})
    assert [event["block_id"] for event in harness.events("user_message")] == [SEND_REQUEST]


async def test_a_held_message_is_queued_and_delivered_under_the_request_id(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    result = await harness.hub.send({"id": SEND_REQUEST, "session_id": "sess-1", "text": "later"})

    assert result == {"accepted": "queued", "queued_id": SEND_REQUEST}
    assert harness.events("queue")[-1]["pending"][0]["id"] == SEND_REQUEST
    await tick(harness.hub, entry, running=False)
    bubbles = harness.events("user_message")
    assert [bubble["block_id"] for bubble in bubbles] == [SEND_REQUEST]
    assert [bubble["delivery"] for bubble in bubbles] == ["delivered"]


# ------------------------------------------- A19: a held message is a queue entry


async def test_a_held_message_is_issued_after_the_turn_it_waited_for(
    harness: Harness,
) -> None:
    """Its `first_seq` comes from the injection, so it lands where the terminal shows it."""
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send({"id": "req-19", "session_id": "sess-1", "text": "later"})
    await entry.channel.emit("assistant_text", block_id="msg-1", text="working", done=True)

    await tick(harness.hub, entry, running=False)
    bubble = harness.events("user_message")[-1]
    said = harness.events("assistant_text")[-1]
    assert bubble["first_seq"] == bubble["seq"], "the block exists from the injection, not before"
    assert bubble["first_seq"] > said["seq"]


async def test_a_held_message_waits_for_the_row_that_really_ends_the_turn(
    harness: Harness,
) -> None:
    """A34: the mirror asks the tailer whether a turn runs, so a row the model
    wrote before its tool calls must not drain what the phone is holding."""
    entry = await harness.attach()
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.awaiting_reply = True
    await tick(harness.hub, entry, running=tailer.busy)
    await harness.hub.send({"id": "req-34", "session_id": "sess-1", "text": "ping"})
    assert harness.attachment.injected == []

    tailer.translate(assistant_row([{"type": "text", "text": "Reading it now."}], "tool_use"))
    await tick(harness.hub, entry, running=tailer.busy)
    assert harness.attachment.injected == [], "the turn had only paused to call a tool"

    tailer.translate(assistant_row([{"type": "text", "text": "Done."}], "end_turn"))
    await tick(harness.hub, entry, running=tailer.busy)
    assert [text for _, text in harness.attachment.injected] == ["ping"]


async def test_a_message_held_across_a_detachment_is_never_shown_twice(
    harness: Harness,
) -> None:
    """No block was published while it waited, so the resume path publishes the first."""
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send({"id": SEND_REQUEST, "session_id": "sess-1", "text": "afterwards"})
    await harness.hub.shared.closed(entry, harness.attachment, "none")

    assert harness.events("user_message") == []
    # The resume path emits the block under the item's own id, which is the
    # request id, so the one bubble it draws is this message's.
    assert [item["block_id"] for item in entry.queue] == [SEND_REQUEST]


# --------------------------------------- A20: a question is answered where you are


async def test_a_question_from_the_hook_is_raised_as_a_block_and_answered_remotely(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)

    pending = harness.events("question")[-1]
    assert pending["status"] == "pending"
    assert [question["id"] for question in pending["questions"]] == ["q0"]
    assert [option["id"] for option in pending["questions"][0]["options"]] == ["o0", "o1"]
    assert entry.session.state == "needs_input"

    assert (
        await harness.hub.answer(
            {
                "session_id": "sess-1",
                "request_id": pending["request_id"],
                "answers": {"q0": ["o1"]},
            }
        )
        == {}
    )
    assert hook.replies == [{PROMPT: "Go monotonic"}]
    resolved = harness.events("question")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["by"] == "remote"
    assert resolved["answers"] == {"q0": ["o1"]}
    assert resolved["block_id"] == pending["block_id"]
    assert harness.events("status")[-1]["state"] == "running", "back to the turn it interrupted"


async def test_the_draft_is_passed_to_the_tool_as_free_text(harness: Harness) -> None:
    await harness.attach()
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)
    request_id = harness.events("question")[-1]["request_id"]

    await harness.hub.answer(
        {"session_id": "sess-1", "request_id": request_id, "answers": {"q0": "neither, widen it"}}
    )
    assert hook.replies == [{PROMPT: "neither, widen it"}]
    assert harness.events("question")[-1]["answers"] == {"q0": "neither, widen it"}


async def test_a_question_answered_in_the_terminal_resolves_by_terminal(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)

    await harness.hub.shared.question_answered(entry, {PROMPT: "Clamp it"})
    assert hook.replies == [None], "the hook steps aside so the dialog's own answer stands"
    resolved = harness.events("question")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["by"] == "terminal"
    assert resolved["answers"] == {"q0": ["o0"]}
    assert entry.session.state != "needs_input"


async def test_an_answer_the_terminal_typed_is_carried_as_free_text(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.attach_question(FakeHookQuestion())
    await harness.hub.shared.question_answered(entry, {PROMPT: "something else entirely"})
    assert harness.events("question")[-1]["answers"] == {"q0": "something else entirely"}


async def test_a_terminal_answer_that_says_nothing_usable_resolves_without_answers(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.attach_question(FakeHookQuestion())
    await harness.hub.shared.question_answered(entry, "not a map")
    resolved = harness.events("question")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["by"] == "terminal"
    assert "answers" not in resolved


async def test_answering_a_question_that_is_no_longer_open_is_a_no_op(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)
    await harness.hub.shared.question_answered(entry, {PROMPT: "Clamp it"})

    before = len(harness.events("question"))
    assert (
        await harness.hub.answer(
            {"session_id": "sess-1", "request_id": "gone", "answers": {"q0": ["o0"]}}
        )
        == {}
    )
    assert len(harness.events("question")) == before
    assert hook.replies == [None]


async def test_the_hook_going_away_expires_the_question(harness: Harness) -> None:
    entry = await harness.attach()
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)
    await harness.hub.attach_question_closed(hook)

    assert harness.events("question")[-1]["status"] == "expired"
    assert entry.session.state != "needs_input"
    state = entry.shared
    assert isinstance(state, SharedState)
    assert state.question is None


async def test_a_message_held_behind_a_question_goes_in_when_the_hook_goes_away(
    harness: Harness,
) -> None:
    """The transcript ended the turn while the dialog was open: nothing else will drain."""
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)
    await harness.hub.send({"id": SEND_REQUEST, "session_id": "sess-1", "text": "later"})
    await tick(harness.hub, entry, running=False)
    assert harness.events("user_message") == []

    await harness.hub.attach_question_closed(hook)
    await drained(entry)

    assert [bubble["block_id"] for bubble in harness.events("user_message")] == [SEND_REQUEST]


async def test_a_second_question_expires_the_one_still_open(harness: Harness) -> None:
    await harness.attach()
    first = FakeHookQuestion()
    await harness.hub.attach_question(first)
    await harness.hub.attach_question(FakeHookQuestion())

    statuses = [event["status"] for event in harness.events("question")]
    assert statuses == ["pending", "expired", "pending"]
    assert first.replies == [None]


async def test_a_question_on_a_session_nothing_is_attached_to_is_declined_at_once(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.shared.closed(entry, harness.attachment, "terminal")

    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)
    assert hook.replies == [None]
    assert harness.events("question") == []

    unknown = FakeHookQuestion(session_id="sess-unknown")
    await harness.hub.attach_question(unknown)
    assert unknown.replies == [None]


async def test_a_detachment_expires_the_question_and_releases_its_hook(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)
    await harness.hub.shared.closed(entry, harness.attachment, "none")

    assert harness.events("question")[-1]["status"] == "expired"
    assert hook.replies == [None]


async def test_a_message_sent_while_a_question_is_open_is_queued_not_injected(
    harness: Harness,
) -> None:
    """An older app still sends; the dialog owns the prompt until it is answered."""
    entry = await harness.attach()
    hook = FakeHookQuestion()
    await harness.hub.attach_question(hook)

    result = await harness.hub.send({"id": "req-20", "session_id": "sess-1", "text": "and then"})
    assert result == {"accepted": "queued", "queued_id": "req-20"}
    assert harness.attachment.injected == []

    await harness.hub.answer(
        {
            "session_id": "sess-1",
            "request_id": harness.events("question")[0]["request_id"],
            "answers": {"q0": ["o0"]},
        }
    )
    await tick(harness.hub, entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["and then"]


async def test_a_question_the_hook_describes_badly_is_declined(harness: Harness) -> None:
    await harness.attach()
    hook = FakeHookQuestion()
    hook.input = {"questions": []}
    await harness.hub.attach_question(hook)
    assert hook.replies == [None]
    assert harness.events("question") == []


async def test_the_answer_row_in_the_transcript_is_recognised() -> None:
    """The tool result is the only account of an answer given in the terminal."""
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.translate(
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [{"type": "tool_use", "id": "t1", "name": QUESTION_TOOL, "input": {}}],
            },
        }
    )
    emits = tailer.translate(
        {
            "type": "user",
            "uuid": "row-3",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            "toolUseResult": {"questions": [], "answers": {PROMPT: "Clamp it"}},
        }
    )
    assert [emit.kind for emit in emits] == ["tool_call", transcripts.QUESTION_ANSWERED]
    assert emits[1].fields == {"answers": {PROMPT: "Clamp it"}}


# ------------------------------------------- A49: files ride in the words as paths


SHOT = {"name": "shot.png", "mime": "image/png", "data_base64": "aGVsbG8="}
NOTES = {"name": "notes.txt", "mime": "text/plain", "data_base64": "aGk="}
SHOT_SHOWN = {"name": "shot.png", "mime": "image/png", "size": 5}


def message_folders() -> list[Path]:
    folder = attachments.state_root() / "sess-1"
    return sorted(folder.iterdir()) if folder.is_dir() else []


async def test_files_reach_the_terminal_as_paths_and_the_apps_as_names(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    result = await harness.hub.send(
        {"id": "req-a", "session_id": "sess-1", "text": "look", "attachments": [SHOT, NOTES]}
    )

    assert result == {"accepted": "sent"}
    [folder] = message_folders()
    message_id, prompt = harness.attachment.injected[0]
    assert prompt == (
        f"look\n\nAttached files:\n- shot.png: {folder / 'shot.png'}\n"
        f"- notes.txt: {folder / 'notes.txt'}"
    )
    assert (folder / "shot.png").read_bytes() == b"hello"
    [bubble] = harness.events("user_message")
    assert (bubble["text"], bubble["delivery"]) == ("look", "delivered")
    assert bubble["attachments"] == [
        SHOT_SHOWN,
        {"name": "notes.txt", "mime": "text/plain", "size": 2},
    ]
    assert str(attachments.state_root()) not in json.dumps(harness.frames)
    assert entry.session.title == "look"

    # Delivered: the files stay where the words point, the bytes are let go.
    await harness.hub.shared.echo_delivered(entry, message_id)
    assert "data_base64" not in json.dumps(entry.sent[message_id]["attachments"])
    assert (folder / "shot.png").exists()


async def test_a_held_message_has_its_files_ready_and_the_queue_counts_them(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    result = await harness.hub.send(
        {"id": "req-b", "session_id": "sess-1", "text": "later", "attachments": [SHOT, SHOT]}
    )

    assert result == {"accepted": "queued", "queued_id": "req-b"}
    assert harness.attachment.injected == []
    [folder] = message_folders()
    assert sorted(path.name for path in folder.iterdir()) == ["shot-2.png", "shot.png"]
    assert harness.events("queue")[-1]["pending"] == [
        {"id": "req-b", "text": "later", "ts": entry.queue[0]["ts"], "attachments": 2}
    ]

    await tick(harness.hub, entry, running=False)
    _, prompt = harness.attachment.injected[0]
    assert prompt == (
        f"later\n\nAttached files:\n- shot.png: {folder / 'shot.png'}\n"
        f"- shot-2.png: {folder / 'shot-2.png'}"
    )
    [bubble] = harness.events("user_message")
    assert [item["name"] for item in bubble["attachments"]] == ["shot.png", "shot-2.png"]
    assert harness.events("queue")[-1]["pending"] == []


async def test_a_held_message_taken_out_of_the_queue_takes_its_files(harness: Harness) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send(
        {"id": "req-c", "session_id": "sess-1", "text": "never mind", "attachments": [SHOT]}
    )
    [folder] = message_folders()

    await harness.hub.queue_remove({"session_id": "sess-1", "queued_id": "req-c"})

    assert not folder.exists()
    assert entry.queue == []


async def test_a_detachment_deletes_held_files_and_delivery_writes_them_again(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send(
        {"id": "req-d", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
    )
    [held] = message_folders()

    await harness.hub.shared.closed(entry, harness.attachment, "terminal")
    assert not held.exists()
    assert harness.events("queue")[-1]["pending"][0]["attachments"] == 1

    harness.attachment = FakeAttachment()
    await harness.attach()
    await tick(harness.hub, entry, running=False)
    [written] = message_folders()
    assert written != held
    assert harness.attachment.injected[0][1] == (
        f"look\n\nAttached files:\n- shot.png: {written / 'shot.png'}"
    )
    assert (written / "shot.png").read_bytes() == b"hello"
    assert harness.events("user_message")[-1]["attachments"] == [SHOT_SHOWN]


async def test_an_absorbed_message_is_sent_again_naming_the_same_files(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.send(
        {"id": "req-e", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
    )
    message_id, first = harness.attachment.injected[0]

    await harness.hub.shared.echo_absorbed(entry, message_id)
    assert harness.events("user_message")[-1]["attachments"] == [SHOT_SHOWN]
    assert harness.events("queue")[-1]["pending"][0]["attachments"] == 1
    await tick(harness.hub, entry, running=False)

    assert harness.attachment.injected[1][1] == first
    assert len(message_folders()) == 1


async def test_a_message_with_a_bad_file_is_refused_before_anything_happens(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    with pytest.raises(RcError) as raised:
        await harness.hub.send(
            {
                "id": "req-f",
                "session_id": "sess-1",
                "text": "look",
                "attachments": [SHOT, {"name": "x", "mime": "text/plain", "data_base64": "!!!"}],
            }
        )
    assert raised.value.code == "bad_request"
    assert harness.attachment.injected == []
    assert entry.queue == []
    assert harness.events("user_message") == []
    assert message_folders() == []


async def test_files_are_refused_where_the_device_says_they_cannot_reach(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.2: `shared_attachments: false`, a shim gone from `PATH` since the CLI started."""
    entry = await harness.attach()
    unready = [replace(info, shared_attachments=False) for info in agents()]
    monkeypatch.setattr(harness.hub, "_agents", lambda: unready)
    with pytest.raises(RcError) as raised:
        await harness.hub.send(
            {"id": "req-g", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
        )
    assert raised.value.code == "unsupported"
    assert harness.attachment.injected == []
    assert entry.queue == []
    assert message_folders() == []


async def test_the_cli_reads_the_session_s_own_file_without_anybody_asked(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    [own] = attachments.materialise("sess-1", [SHOT])
    await harness.hub.attach_permission_request(
        harness.attachment,
        {
            "request_id": "rown1",
            "tool_name": "Read",
            "description": "Read a file from the local filesystem.",
            "input_preview": json.dumps({"file_path": own.path}),
        },
    )

    assert harness.attachment.verdicts == [("rown1", "allow")]
    assert harness.events("approval") == []
    assert entry.session.state != "needs_approval"


async def test_the_preview_claude_renders_with_spaces_is_read_as_the_input(
    harness: Harness,
) -> None:
    """Claude 2.1.299 writes the preview as `{ "file_path": "..." }`."""
    await harness.attach()
    [own] = attachments.materialise("sess-1", [SHOT])
    await harness.hub.attach_permission_request(
        harness.attachment,
        {
            "request_id": "rspaced",
            "tool_name": "Read",
            "description": "Read a file from the local filesystem.",
            "input_preview": f'{{ "file_path": "{own.path}" }}',
        },
    )
    assert harness.attachment.verdicts == [("rspaced", "allow")]
    assert harness.events("approval") == []


async def test_reading_another_session_s_file_still_asks(harness: Harness) -> None:
    await harness.attach()
    [other] = attachments.materialise("sess-2", [SHOT])
    await harness.hub.attach_permission_request(
        harness.attachment,
        {
            "request_id": "rother1",
            "tool_name": "Read",
            "description": "Read a file from the local filesystem.",
            "input_preview": json.dumps({"file_path": other.path}),
        },
    )

    assert harness.attachment.verdicts == []
    assert harness.events("approval")[-1]["status"] == "pending"


def test_a_prompt_read_back_from_a_transcript_names_its_files_and_no_path() -> None:
    """A remote session's own transcript, mirrored once a terminal resumes it."""
    written = attachments.materialise("sess-1", [SHOT])
    row = {
        "type": "user",
        "uuid": "row-9",
        "message": {"role": "user", "content": attachments.describe("look", written)},
    }
    [emit] = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo").translate(row)
    assert emit.kind == "user_message"
    assert emit.fields == {
        "block_id": "row-9",
        "text": "look",
        "source": "terminal",
        "attachments": [SHOT_SHOWN],
    }


async def test_a_held_message_whose_files_were_swept_writes_them_again(
    harness: Harness,
) -> None:
    """The size cap may take a waiting message's files; its kept bytes put them back."""
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send(
        {"id": "req-h", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
    )
    [swept] = message_folders()
    (swept / "shot.png").unlink()

    await tick(harness.hub, entry, running=False)

    [written] = message_folders()
    assert (written / "shot.png").read_bytes() == b"hello"
    assert harness.attachment.injected[0][1].endswith(f"- shot.png: {written / 'shot.png'}")


async def test_a_send_that_fails_before_it_is_held_leaves_no_files(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rc_client.sessions import titles

    await harness.attach()

    async def broken(channel: Any, text: str, files: Any = None) -> bool:
        raise RuntimeError("the summary could not be published")

    monkeypatch.setattr(titles, "from_prompt", broken)
    with pytest.raises(RuntimeError):
        await harness.hub.send(
            {"id": "req-i", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
        )
    assert message_folders() == []


class DetachingAttachment(FakeAttachment):
    """A bridge that goes away while a held message is being handed to it."""

    def __init__(self, hub: SessionHub) -> None:
        super().__init__()
        self.hub = hub

    async def inject(self, message_id: str, text: str) -> bool:
        entry = self.hub.entry("sess-1")
        await self.hub.shared.closed(entry, self, "terminal")
        return False


async def test_a_message_held_back_by_a_detachment_keeps_no_files(harness: Harness) -> None:
    harness.attachment = DetachingAttachment(harness.hub)
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send(
        {"id": "req-j", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
    )

    await tick(harness.hub, entry, running=False)

    assert [item["id"] for item in entry.queue] == ["req-j"]
    assert message_folders() == [], "whatever delivers it later writes them again"


@pytest.mark.parametrize(
    "preview",
    [
        '{"file_path": "/truncated/at/the/end',
        "Read a file from the local filesystem",
        '["not", "an", "object"]',
        '{"file_path": "\\u0000"}',
        "",
    ],
)
async def test_a_preview_the_device_cannot_read_is_asked_about(
    harness: Harness, preview: str
) -> None:
    await harness.attach()
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "rlossy", "tool_name": "Read", "description": "", "input_preview": preview},
    )
    assert harness.attachment.verdicts == []
    assert harness.events("approval")[-1]["status"] == "pending"


async def test_editing_the_session_s_own_file_still_asks(harness: Harness) -> None:
    await harness.attach()
    [own] = attachments.materialise("sess-1", [SHOT])
    await harness.hub.attach_permission_request(
        harness.attachment,
        {
            "request_id": "redit1",
            "tool_name": "Edit",
            "description": "Edit a file",
            "input_preview": json.dumps({"file_path": own.path, "old_string": "a"}),
        },
    )
    assert harness.attachment.verdicts == []
    assert harness.events("approval")[-1]["status"] == "pending"


async def test_stopping_the_daemon_deletes_the_files_of_held_messages(harness: Harness) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send(
        {"id": "req-k", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
    )
    assert len(message_folders()) == 1

    await harness.hub.close()

    assert message_folders() == []


@pytest.mark.parametrize("waiting", [False, True], ids=["before-the-drain", "already-waiting"])
async def test_a_message_sent_as_the_turn_ends_goes_after_the_one_held_for_it(
    harness: Harness, waiting: bool
) -> None:
    """The turn's end only schedules the drain (A43).

    A send that takes the session's lock before the drain task, or was already
    waiting for it when the turn ended, hands the held message over first.
    """
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send({"id": "req-x", "session_id": "sess-1", "text": "x"})
    sending = harness.hub.send({"id": "req-y", "session_id": "sess-1", "text": "y"})
    if waiting:
        async with entry.lock:
            sent = asyncio.create_task(sending)
            for _ in range(10):
                await asyncio.sleep(0)
            await harness.hub.shared.tick(entry, running=False)
        result = await sent
    else:
        await harness.hub.shared.tick(entry, running=False)
        result = await sending
    await drained(entry)

    assert result == {"accepted": "queued", "queued_id": "req-y"}
    assert [text for _, text in harness.attachment.injected] == ["x"]
    assert [item["id"] for item in entry.queue] == ["req-y"]


async def test_two_turn_ends_at_once_hand_over_held_messages_in_order(harness: Harness) -> None:
    """While the first held message's files are checked, the second must wait (A19, A43)."""
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    await harness.hub.send(
        {"id": "req-x", "session_id": "sess-1", "text": "x", "attachments": [SHOT]}
    )
    await harness.hub.send({"id": "req-y", "session_id": "sess-1", "text": "y"})

    await asyncio.gather(
        tick(harness.hub, entry, running=False),
        tick(harness.hub, entry, running=False),
    )

    assert [text.split("\n")[0] for _, text in harness.attachment.injected] == ["x"]
    assert [item["id"] for item in entry.queue] == ["req-y"]


async def test_a_message_the_bridge_refuses_on_sending_keeps_no_files(harness: Harness) -> None:
    """A bridge refuses only as it goes away; delivery later writes the files again."""
    entry = await harness.attach()
    harness.attachment.reachable = False
    result = await harness.hub.send(
        {"id": "req-l", "session_id": "sess-1", "text": "look", "attachments": [SHOT]}
    )
    assert result == {"accepted": "queued", "queued_id": "req-l"}
    assert message_folders() == []

    harness.attachment.reachable = True
    await tick(harness.hub, entry, running=False)
    [written] = message_folders()
    assert harness.attachment.injected[0][1].endswith(f"- shot.png: {written / 'shot.png'}")


# ------------------------------------------------- A54: a plan from the hook

PLAN_TEXT = "# Add retries\n\n- wrap the client\n"


class FakePlanHook(HookQuestion):
    def __init__(self, truncated: bool = False) -> None:
        super().__init__(
            session_id="sess-1",
            cwd="/repo",
            tool="ExitPlanMode",
            input={"plan": PLAN_TEXT, "plan_truncated": truncated},
        )
        self.replies: list[dict[str, Any] | None] = []

    async def answer(self, answers: dict[str, Any] | None) -> bool:
        self.replies.append(answers)
        return True


async def test_a_plan_from_the_hook_is_raised_as_a_plan_card(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.attach_question(FakePlanHook())

    approval = harness.events("approval")[-1]
    assert approval["status"] == "pending"
    assert approval["approval_kind"] == "plan"
    assert approval["input"] == {"plan": PLAN_TEXT}
    assert approval["title"] == "Plan ready: Add retries"
    assert [option["id"] for option in approval["options"]] == [
        "plan_accept_edits",
        "plan_manual",
        "plan_keep",
    ]
    assert entry.session.state == "needs_approval"


async def test_approving_a_plan_answers_the_hook_with_the_mode(harness: Harness) -> None:
    entry = await harness.attach()
    hook = FakePlanHook()
    await harness.hub.attach_question(hook)
    approval = harness.events("approval")[-1]

    await harness.hub.approve(
        {
            "session_id": "sess-1",
            "request_id": approval["request_id"],
            "option_id": "plan_accept_edits",
        }
    )

    assert hook.replies == [{"behavior": "allow", "mode": "acceptEdits"}]
    resolved = harness.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "plan_accept_edits", "by": "remote"}
    assert resolved["approval_kind"] == "plan"
    assert entry.session.state != "needs_approval"


async def test_keeping_on_planning_carries_the_feedback_to_the_hook(harness: Harness) -> None:
    await harness.attach()
    hook = FakePlanHook()
    await harness.hub.attach_question(hook)
    approval = harness.events("approval")[-1]

    await harness.hub.approve(
        {
            "session_id": "sess-1",
            "request_id": approval["request_id"],
            "option_id": "plan_keep",
            "message": "use a queue",
        }
    )

    [reply] = hook.replies
    assert reply is not None
    assert reply["behavior"] == "deny"
    assert "use a queue" in reply["message"]
    assert reply["interrupt"] is False


async def test_a_plan_card_refuses_an_option_it_never_offered(harness: Harness) -> None:
    await harness.attach()
    hook = FakePlanHook()
    await harness.hub.attach_question(hook)
    approval = harness.events("approval")[-1]

    with pytest.raises(RcError) as raised:
        await harness.hub.approve(
            {"session_id": "sess-1", "request_id": approval["request_id"], "option_id": "allow"}
        )
    assert raised.value.code == "bad_request"
    assert hook.replies == []


async def test_a_plan_too_big_to_show_whole_offers_no_approval(harness: Harness) -> None:
    await harness.attach()
    hook = FakePlanHook(truncated=True)
    await harness.hub.attach_question(hook)
    approval = harness.events("approval")[-1]

    assert approval["plan_truncated"] is True
    assert [option["id"] for option in approval["options"]] == ["plan_shorten", "plan_keep"]
    with pytest.raises(RcError):
        await harness.hub.approve(
            {
                "session_id": "sess-1",
                "request_id": approval["request_id"],
                "option_id": "plan_accept_edits",
            }
        )


async def test_a_plan_the_terminal_answered_resolves_from_the_transcript(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    hook = FakePlanHook()
    await harness.hub.attach_question(hook)
    await harness.hub.attach_question_closed(hook)

    assert harness.events("approval")[-1]["status"] == "pending"
    await harness.hub.shared.tool_finished(entry, "ExitPlanMode", failed=False)

    resolved = harness.events("approval")[-1]
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "elsewhere", "by": "terminal"}


async def test_answering_a_plan_the_hook_already_left_changes_nothing(harness: Harness) -> None:
    await harness.attach()
    hook = FakePlanHook()
    await harness.hub.attach_question(hook)
    approval = harness.events("approval")[-1]
    await harness.hub.attach_question_closed(hook)

    before = len(harness.events("approval"))
    result = await harness.hub.approve(
        {
            "session_id": "sess-1",
            "request_id": approval["request_id"],
            "option_id": "plan_manual",
        }
    )
    assert result == {}
    assert len(harness.events("approval")) == before
    assert hook.replies == []


async def test_a_plan_with_no_text_steps_aside(harness: Harness) -> None:
    await harness.attach()
    hook = FakePlanHook()
    hook.input = {"plan": ""}
    await harness.hub.attach_question(hook)
    assert harness.events("approval") == []
    assert hook.replies == [None]


async def test_a_choice_the_hook_can_no_longer_take_is_not_resolved(harness: Harness) -> None:
    await harness.attach()

    class GoneHook(FakePlanHook):
        async def answer(self, answers: dict[str, Any] | None) -> bool:
            return False

    await harness.hub.attach_question(GoneHook())
    approval = harness.events("approval")[-1]
    before = len(harness.events("approval"))

    await harness.hub.approve(
        {"session_id": "sess-1", "request_id": approval["request_id"], "option_id": "plan_manual"}
    )

    assert len(harness.events("approval")) == before


async def test_a_plan_from_the_hook_is_fitted_to_the_frame_again(harness: Harness) -> None:
    await harness.attach()
    hook = FakePlanHook()
    hook.input = {"plan": "word " * 40000, "plan_truncated": False}
    await harness.hub.attach_question(hook)

    approval = harness.events("approval")[-1]
    assert approval["plan_truncated"] is True
    assert len(json.dumps(approval["input"]["plan"]).encode("utf-8")) <= 48 * 1024
    assert [option["id"] for option in approval["options"]] == ["plan_shorten", "plan_keep"]


# ------------------------------------------------- the usage-limit menu (A63)

LIMIT_NOW = 1_800_000_000_000
RESET = LIMIT_NOW + 60 * 60 * 1000
# A held message goes a minute after the reset, as A35's resume does.
AFTER_RESET = RESET + limit_dialog.RESET_LEAD_MS
CLOSE_OPTION = {
    "id": "close",
    "label": limit_dialog.CLOSE_LABEL,
    "description": limit_dialog.CLOSE_DESCRIPTION,
}
CONTINUES_OUTPUT = (
    "<local-command-stdout>Claude Code will continue automatically when the limit resets"
    "</local-command-stdout>"
)


@pytest.fixture
def menu_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    quick(monkeypatch)


async def limited(
    harness: Harness, terminal: LimitTerminal | None = None, clock: Clock | None = None
) -> SessionEntry:
    """An attached session, its terminal if it has one, and the clock its reset is read on."""
    entry = await harness.attach()
    if terminal is not None:
        harness.hub.ptys.add(terminal)
    harness.hub.shared.clock = clock or Clock(LIMIT_NOW)
    return entry


async def stop_at_limit(
    harness: Harness,
    entry: SessionEntry,
    *,
    hook: bool = True,
    row: bool = True,
    resets: int | None = RESET,
) -> None:
    """A turn the usage limit stops: the `StopFailure` hook, then the transcript's 429 row."""
    await tick(harness.hub, entry, running=True)
    if hook:
        await harness.hub.shared.turn_hook(
            entry, TurnHook(session_id="sess-1", event="limit", said=SAID)
        )
    if row:
        written = harness.hub.shared.clock()
        limit = LimitStop(300, resets, said=SAID, at_ms=written)
        await tick(harness.hub, entry, running=False, end=TurnEnd("error", limit))


def cards(harness: Harness) -> list[dict[str, Any]]:
    return harness.events("question")


def options(card: dict[str, Any]) -> list[dict[str, Any]]:
    return list(card["questions"][0]["options"])


async def card_up(harness: Harness) -> dict[str, Any]:
    await until(lambda: any(card["status"] == "pending" for card in cards(harness)))
    return cards(harness)[-1]


async def answer_card(harness: Harness, card: dict[str, Any], answer: Any) -> dict[str, Any]:
    return await harness.hub.answer(
        {"session_id": "sess-1", "request_id": card["request_id"], "answers": {"q0": answer}}
    )


def menu_over(entry: SessionEntry) -> bool:
    return entry.shared is not None and entry.shared.limit is None


def shown(entry: SessionEntry) -> str:
    """The state the apps are shown, read afresh each time."""
    return str(entry.session.state)


def injected(harness: Harness) -> list[str]:
    return [text for _, text in harness.attachment.injected]


async def test_the_menu_is_a_card_of_its_free_rows_close_and_room_for_words(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)

    card = await card_up(harness)
    [question] = card["questions"]
    assert question["prompt"] == f"{SAID}\n\n{limit_dialog.WAITING}"
    # The free options on screen, in screen order, in the CLI's own words; never credits.
    assert question["options"] == [
        {"id": "stop_wait", "label": STOP_WAIT},
        {"id": "wait", "label": WAIT_AT},
        CLOSE_OPTION,
    ]
    assert (question["multi"], question["allow_text"]) == (False, True)
    assert shown(entry) == "needs_input"
    [ended] = harness.events("turn_completed")
    assert (ended["stop_reason"], ended["limit"]["resets_at"]) == ("error", RESET)
    assert terminal.typed == []


async def test_a_row_the_person_picks_is_pressed_by_its_number_on_screen(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    assert await answer_card(harness, card, ["wait"]) == {}

    assert terminal.typed == ["2"]
    assert terminal.chosen == WAIT_AT
    resolved = cards(harness)[-1]
    assert (resolved["status"], resolved["by"], resolved["answers"]) == (
        "resolved",
        "remote",
        {"q0": ["wait"]},
    )
    assert resolved["block_id"] == card["block_id"]
    assert shown(entry) == "idle"
    assert menu_over(entry)
    # What Claude Code records for that choice is the device's key, not a command typed there.
    assert await harness.hub.shared.typed_command(entry, "/rate-limit-options")
    validator = event_validator()
    if validator is not None:
        for frame in harness.frames:
            if frame.get("type") == "session.event":
                validator.validate(frame["event"])


async def test_close_is_a_lone_escape(harness: Harness, menu_timing: None) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    await answer_card(harness, card, ["close"])

    assert terminal.typed == [limit_dialog.ESCAPE]
    assert terminal.chosen == "escape"
    resolved = cards(harness)[-1]
    assert (resolved["by"], resolved["answers"]) == ("remote", {"q0": ["close"]})
    assert menu_over(entry) and injected(harness) == []


async def test_words_on_the_card_close_the_menu_and_go_as_a_message(
    harness: Harness, menu_timing: None
) -> None:
    """The person writes "go on" where the card asks: that is a message, not a row."""
    clock = Clock(AFTER_RESET)
    terminal = LimitTerminal()
    entry = await limited(harness, terminal, clock)
    clock.now = LIMIT_NOW
    await stop_at_limit(harness, entry)
    card = await card_up(harness)
    clock.now = AFTER_RESET

    assert await answer_card(harness, card, "go on") == {}

    assert terminal.typed == [limit_dialog.ESCAPE]
    resolved = cards(harness)[-1]
    assert (resolved["status"], resolved["by"], resolved["answers"]) == (
        "resolved",
        "remote",
        {"q0": "go on"},
    )
    assert injected(harness) == ["go on"]
    assert harness.events("user_message")[-1]["text"] == "go on"


async def test_words_before_the_reset_close_the_menu_and_wait_a_minute_past_it(
    harness: Harness, menu_timing: None
) -> None:
    clock = Clock(LIMIT_NOW)
    terminal = LimitTerminal()
    entry = await limited(harness, terminal, clock)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    await answer_card(harness, card, "go on")

    assert terminal.typed == [limit_dialog.ESCAPE] and menu_over(entry)
    assert injected(harness) == []
    assert harness.events("queue")[-1]["pending"][0]["text"] == "go on"
    clock.now = RESET
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == [], "the reset itself is a minute too early"
    clock.now = AFTER_RESET
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["go on"]


async def test_a_row_whose_words_changed_is_refused_and_the_card_shows_the_menu_now(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    terminal.redraw((STOP_WAIT, WAIT_SHORTLY, CREDITS))
    with pytest.raises(RcError) as caught:
        await answer_card(harness, card, ["wait"])

    assert (caught.value.code, caught.value.message) == ("conflict", limit_dialog.CHANGED)
    assert terminal.typed == []
    refreshed = cards(harness)[-1]
    assert (refreshed["status"], refreshed["request_id"]) == ("pending", card["request_id"])
    assert {"id": "wait", "label": WAIT_SHORTLY} in options(refreshed)

    await answer_card(harness, refreshed, ["wait"])
    assert terminal.typed == ["2"] and terminal.chosen == WAIT_SHORTLY
    assert menu_over(entry)


async def test_a_key_the_menu_ignored_is_pressed_once_more(
    harness: Harness, menu_timing: None
) -> None:
    """Claude Code refuses a choice within 250 ms of a draw or a key."""
    terminal = LimitTerminal(swallow=1)
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    await answer_card(harness, card, ["stop_wait"])

    assert terminal.typed == ["1", "1"]
    assert terminal.chosen == STOP_WAIT
    assert cards(harness)[-1]["by"] == "remote"
    assert menu_over(entry)


async def test_a_menu_that_ignores_both_keys_is_left_to_the_terminal(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal(swallow=2)
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    await answer_card(harness, card, ["stop_wait"])

    assert terminal.typed == ["1", "1"]
    assert cards(harness)[-1]["status"] == "expired"
    assert harness.events("notice")[-1]["text"] == limit_dialog.FAILED
    assert shown(entry) == "needs_input"


async def test_an_answer_given_in_the_terminal_first_resolves_by_terminal(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    terminal.person_moves()
    await asyncio.sleep(0.1)
    assert cards(harness)[-1]["status"] == "pending", "moving the highlight answers nothing"

    terminal.person_answers(1)
    await until(lambda: cards(harness)[-1]["status"] == "resolved")
    resolved = cards(harness)[-1]
    assert resolved["by"] == "terminal" and "answers" not in resolved
    assert shown(entry) == "idle"
    # A late answer from an app changes nothing.
    assert await answer_card(harness, card, ["stop_wait"]) == {}
    assert terminal.typed == []


async def test_a_terminal_that_goes_away_expires_the_card(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    await card_up(harness)

    harness.hub.ptys.remove(terminal)

    await until(lambda: cards(harness)[-1]["status"] == "expired")
    assert menu_over(entry)
    assert terminal.typed == []


async def test_a_menu_the_screen_no_longer_shows_is_left_to_the_terminal(
    harness: Harness, menu_timing: None
) -> None:
    """Nothing to read the row's number from: the card expires and the person is told why."""
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    terminal.text = "the screen has scrolled far past anything the menu drew\n"
    await answer_card(harness, card, ["stop_wait"])

    assert terminal.typed == []
    assert cards(harness)[-1]["status"] == "expired"
    assert harness.events("notice")[-1]["text"] == limit_dialog.FAILED


async def test_an_option_the_card_did_not_offer_is_refused(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)

    for answer in (["credits"], ["escape"], ["wait", "stop_wait"], [], "  ", None):
        with pytest.raises(RcError) as caught:
            await answer_card(harness, card, answer)
        assert caught.value.code == "bad_request"
    assert terminal.typed == []
    assert entry.shared is not None and entry.shared.limit is not None


# ------------------------------------------------ messages while the menu is up


async def test_a_message_closes_the_menu_with_a_lone_escape_and_waits_for_the_reset(
    harness: Harness, menu_timing: None
) -> None:
    clock = Clock(LIMIT_NOW)
    terminal = LimitTerminal()
    entry = await limited(harness, terminal, clock)
    await stop_at_limit(harness, entry)
    await card_up(harness)

    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})

    # Escape on its own: nothing after it that would make it a sequence.
    assert terminal.typed == [limit_dialog.ESCAPE]
    resolved = cards(harness)[-1]
    assert (resolved["by"], resolved["answers"]) == ("remote", {"q0": ["close"]})
    assert result == {"accepted": "queued", "queued_id": "req-1"}
    assert injected(harness) == []
    assert harness.events("queue")[-1]["pending"][0]["text"] == "go on"

    clock.now = AFTER_RESET
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["go on"]


async def test_a_message_past_the_reset_closes_the_menu_and_goes(
    harness: Harness, menu_timing: None
) -> None:
    clock = Clock(LIMIT_NOW)
    terminal = LimitTerminal()
    entry = await limited(harness, terminal, clock)
    await stop_at_limit(harness, entry)
    await card_up(harness)

    clock.now = AFTER_RESET
    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})

    assert result == {"accepted": "sent"}
    assert terminal.typed == [limit_dialog.ESCAPE]
    assert injected(harness) == ["go on"]
    assert menu_over(entry)


async def test_a_message_with_no_known_reset_goes_once_the_menu_is_closed(
    harness: Harness, menu_timing: None
) -> None:
    """The CLI named no time: nothing is held for a reset nobody knows."""
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry, resets=None)
    await card_up(harness)

    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})

    assert terminal.typed == [limit_dialog.ESCAPE]
    assert result == {"accepted": "sent"} and injected(harness) == ["go on"]


async def test_the_messages_held_before_go_first(harness: Harness, menu_timing: None) -> None:
    clock = Clock(LIMIT_NOW)
    entry = await limited(harness, LimitTerminal(), clock)
    await stop_at_limit(harness, entry)
    await card_up(harness)
    await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "first"})

    clock.now = AFTER_RESET
    result = await harness.hub.send({"id": "req-2", "session_id": "sess-1", "text": "second"})

    assert result == {"accepted": "queued", "queued_id": "req-2"}
    assert injected(harness) == ["first"]


async def test_a_message_while_the_person_types_there_leaves_the_menu_to_them(
    harness: Harness, menu_timing: None
) -> None:
    """Escape is only pressed into a terminal whose keyboard has been still for two seconds."""
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    await card_up(harness)

    terminal.person_types()
    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})

    assert result["accepted"] == "queued"
    assert terminal.typed == [] and terminal.open
    assert cards(harness)[-1]["status"] == "pending"
    assert shown(entry) == "needs_input"


async def test_a_screen_that_moved_between_the_look_and_the_key_takes_no_escape(
    harness: Harness, menu_timing: None
) -> None:
    """The proxy types only while the screen ends as the device read it: here it no longer does."""
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    await card_up(harness)

    terminal.drawn_before_key = PROMPT_BOX
    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})

    assert terminal.withheld == [limit_dialog.ESCAPE]
    assert terminal.typed == []
    assert result["accepted"] == "queued" and injected(harness) == []
    assert cards(harness)[-1]["status"] == "pending"


# ------------------------------------------------ what the device presses itself


async def test_a_message_never_presses_a_row_whatever_the_first_row_is(
    harness: Harness, menu_timing: None
) -> None:
    """Under an experiment the first row costs money; the device's own key is Escape, always."""
    clock = Clock(LIMIT_NOW)
    terminal = LimitTerminal((CREDITS, WAIT_AT, STOP_WAIT))
    entry = await limited(harness, terminal, clock)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)
    assert [option["id"] for option in options(card)] == ["wait", "stop_wait", "close"]

    clock.now = AFTER_RESET
    await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})

    assert terminal.typed == [limit_dialog.ESCAPE]
    assert terminal.chosen == "escape"


async def test_resuming_after_the_limit_picks_wait_here_by_its_words(
    harness: Harness, menu_timing: None
) -> None:
    """A35's switch: never a fixed number, and never the paid row above it."""
    await harness.hub.resumes.set_enabled(True)
    terminal = LimitTerminal((CREDITS, WAIT_AT, STOP_WAIT))
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)

    await until(lambda: terminal.chosen is not None)
    assert terminal.typed == ["2"]
    assert terminal.chosen == WAIT_AT
    await until(lambda: menu_over(entry))
    assert cards(harness) == []
    assert WAIT_AT in harness.events("notice")[-1]["text"]
    assert shown(entry) == "idle"


async def test_wait_here_the_screen_broke_is_not_picked_and_the_card_goes_up(
    harness: Harness, menu_timing: None
) -> None:
    await harness.hub.resumes.set_enabled(True)
    terminal = LimitTerminal()
    terminal.text = CONVERSATION + menu_text((STOP_WAIT, "Wait here, then contin e automatically"))
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)

    card = await card_up(harness)
    assert [option["id"] for option in options(card)] == ["stop_wait", "wait", "close"]
    await asyncio.sleep(0.1)
    assert terminal.typed == []
    assert entry.shared is not None and entry.shared.limit is not None


async def test_resuming_after_the_limit_without_wait_here_raises_the_card(
    harness: Harness, menu_timing: None
) -> None:
    await harness.hub.resumes.set_enabled(True)
    terminal = LimitTerminal((STOP_WAIT, CREDITS))
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    card = await card_up(harness)
    assert [option["id"] for option in options(card)] == ["stop_wait", "close"]
    await asyncio.sleep(0.1)
    assert terminal.typed == []
    assert entry.shared is not None and entry.shared.limit is not None


async def test_claude_code_saying_it_continues_by_itself_cancels_the_device_s_resume(
    harness: Harness, menu_timing: None
) -> None:
    """Only its own word after "Wait here" does: a second continue is what is avoided."""
    await harness.hub.resumes.set_enabled(True)
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)
    await until(lambda: terminal.chosen is not None)
    assert [event["status"] for event in harness.events("resume")] == ["scheduled"]

    assert await harness.hub.shared.typed_command(entry, "/rate-limit-options")
    await harness.hub.shared.command_output(entry, CONTINUES_OUTPUT)

    cancelled = harness.events("resume")[-1]
    assert (cancelled["status"], cancelled["reason"]) == (
        "cancelled",
        "Claude Code continues by itself once the limit resets",
    )
    assert entry.session.resume is None


async def test_without_claude_code_s_word_the_device_s_resume_stays(
    harness: Harness, menu_timing: None
) -> None:
    await harness.hub.resumes.set_enabled(True)
    entry = await limited(harness, LimitTerminal((STOP_WAIT, CREDITS)))
    await stop_at_limit(harness, entry)
    await card_up(harness)
    await harness.hub.shared.command_output(
        entry, "<local-command-stdout>Done</local-command-stdout>"
    )
    assert [event["status"] for event in harness.events("resume")] == ["scheduled"]
    assert entry.session.resume is not None


# ------------------------------------------------------------------- seeing it


async def test_the_hook_and_the_transcript_row_of_one_stop_raise_one_card(
    harness: Harness, menu_timing: None
) -> None:
    entry = await limited(harness, LimitTerminal())
    await stop_at_limit(harness, entry)
    await card_up(harness)
    await tick(harness.hub, entry, running=False, end=TurnEnd("error", LimitStop(300, RESET)))
    assert len({card["request_id"] for card in cards(harness)}) == 1
    state = entry.shared
    assert state is not None and state.limit is not None and state.limit.resets_at_ms == RESET


async def test_the_transcript_row_alone_raises_the_card(
    harness: Harness, menu_timing: None
) -> None:
    """A Claude Code without the hook, or one whose hook was not heard: the 429 row is enough."""
    entry = await limited(harness, LimitTerminal())
    await stop_at_limit(harness, entry, hook=False)
    card = await card_up(harness)
    assert card["questions"][0]["prompt"].startswith(SAID)
    # The stop is the turn's first: it ends with the limit, which A35 schedules from.
    [ended] = harness.events("turn_completed")
    assert (ended["stop_reason"], ended["limit"]["resets_at"]) == ("error", RESET)
    assert shown(entry) == "needs_input"


async def test_an_injection_the_cli_queued_where_it_looked_idle_finds_the_menu(
    harness: Harness, menu_timing: None
) -> None:
    """No stop was heard, yet the CLI queued what went into an idle prompt: a dialog holds it."""
    terminal = LimitTerminal()
    entry = await limited(harness, terminal)
    assert await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"}) == {
        "accepted": "sent"
    }
    [(message_id, _)] = harness.attachment.injected

    await harness.hub.shared.echo_queued(entry, message_id)

    card = await card_up(harness)
    assert card["questions"][0]["prompt"].startswith(limit_dialog.LIMIT_REACHED)
    assert shown(entry) == "needs_input"
    assert terminal.typed == []


async def test_a_queued_injection_with_no_menu_on_screen_says_nothing(
    harness: Harness, menu_timing: None
) -> None:
    entry = await limited(harness, LimitTerminal(("Allow", "Deny"), shows=True))
    await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"})
    [(message_id, _)] = harness.attachment.injected

    await harness.hub.shared.echo_queued(entry, message_id)

    await until(lambda: menu_over(entry))
    assert cards(harness) == [] and harness.events("notice") == []


async def test_a_menu_with_rows_nobody_knows_is_said_and_never_typed_into(
    harness: Harness, menu_timing: None
) -> None:
    terminal = LimitTerminal(("Something new", "Something else"))
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)

    await until(lambda: bool(harness.events("notice")))
    notice = harness.events("notice")[-1]
    assert (notice["level"], notice["text"]) == ("warn", limit_dialog.NOTICE)
    assert cards(harness) == []
    assert shown(entry) == "needs_input"
    assert terminal.typed == []
    with pytest.raises(RcError) as caught:
        await harness.hub.shared.set_settings(entry, "opus", None)
    assert caught.value.code == "conflict"


async def test_a_session_with_no_terminal_of_the_device_s_is_told_and_given_no_card(
    harness: Harness, menu_timing: None
) -> None:
    """Claude Code's own record says a dialog is open; nothing can be typed into it."""
    entry = await limited(harness)
    write_record(4242, "sess-1", status="waiting", waitingFor="dialog open")
    await stop_at_limit(harness, entry, hook=False)

    await until(lambda: bool(harness.events("notice")))
    assert harness.events("notice")[-1]["text"] == limit_dialog.NOTICE
    assert cards(harness) == []
    assert shown(entry) == "needs_input"

    write_record(4242, "sess-1", status="idle")
    await until(lambda: menu_over(entry))
    assert shown(entry) == "idle"


async def test_a_stop_with_no_menu_after_it_raises_nothing(
    harness: Harness, menu_timing: None
) -> None:
    """Claude Code had a continue left: it waits for the reset and goes on by itself."""
    terminal = LimitTerminal(shows=False)
    entry = await limited(harness, terminal)
    await stop_at_limit(harness, entry)

    await until(lambda: menu_over(entry))
    assert cards(harness) == []
    assert harness.events("notice") == []
    assert shown(entry) == "idle"
    assert terminal.typed == []


async def test_a_stop_read_long_after_it_was_written_opens_no_menu(
    harness: Harness, menu_timing: None
) -> None:
    entry = await limited(harness, LimitTerminal())
    old = LimitStop(300, RESET, said=SAID, at_ms=LIMIT_NOW - 60 * 60 * 1000)
    await tick(harness.hub, entry, running=False, end=TurnEnd("error", old))
    state = entry.shared
    assert state is not None and state.limit is None
    assert state.limit_until_ms == RESET


async def test_nothing_held_goes_in_until_a_minute_after_the_reset(
    harness: Harness, menu_timing: None
) -> None:
    """It would only stop at the limit again, and bring the menu back."""
    clock = Clock(LIMIT_NOW)
    entry = await limited(harness, LimitTerminal(shows=False), clock)
    await stop_at_limit(harness, entry)
    await until(lambda: menu_over(entry))

    result = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go on"})
    assert result["accepted"] == "queued"
    clock.now = RESET
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == []

    clock.now = AFTER_RESET
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["go on"]


async def test_a_stop_at_the_limit_sends_nothing_again(harness: Harness, menu_timing: None) -> None:
    """The limit hook is no step past what was handed over: nothing is re-sent for a 429."""
    entry = await limited(harness, LimitTerminal(shows=False))
    await tick(harness.hub, entry, running=True)
    state = entry.shared
    assert state is not None
    handed = shared_item("also this")
    state.handed[str(handed["message_id"])] = handed

    await harness.hub.shared.turn_hook(
        entry, TurnHook(session_id="sess-1", event="limit", said=SAID)
    )

    assert "passed_at" not in handed


async def test_what_was_steered_into_the_stopped_turn_waits_in_the_queue(
    harness: Harness, menu_timing: None
) -> None:
    entry = await limited(harness, LimitTerminal(shows=False))
    await tick(harness.hub, entry, running=True)
    state = entry.shared
    assert state is not None
    state.steers.append(shared_item("also this"))

    await harness.hub.shared.turn_hook(
        entry, TurnHook(session_id="sess-1", event="limit", said=SAID)
    )

    assert state.steers == []
    assert [item["text"] for item in entry.queue] == ["also this"]
    assert harness.events("queue")[-1]["pending"][0]["text"] == "also this"


async def test_a_turn_the_cli_begins_ends_the_menu_and_the_wait(
    harness: Harness, menu_timing: None
) -> None:
    """The person chose in the terminal and went on, or the CLI continued by itself."""
    entry = await limited(harness, LimitTerminal())
    await stop_at_limit(harness, entry)
    await card_up(harness)

    await tick(harness.hub, entry, running=True)

    assert cards(harness)[-1]["by"] == "terminal"
    state = entry.shared
    assert state is not None and state.limit is None and state.limit_until_ms is None


async def test_a_detachment_expires_the_menu_s_card(harness: Harness, menu_timing: None) -> None:
    entry = await limited(harness, LimitTerminal())
    await stop_at_limit(harness, entry)
    await card_up(harness)

    await harness.hub.attach_closed(harness.attachment)

    assert cards(harness)[-1]["status"] == "expired"
    assert entry.shared is None


def shared_item(text: str) -> dict[str, Any]:
    from rc_client.sessions.shared import pending_item

    return pending_item(text, f"req-{text}")


def test_the_menu_text_helper_draws_what_the_device_reads() -> None:
    """The fake terminal's menu is one the device recognises, so the flows above test it."""
    from rc_client.sessions.limit_menu import read_menu

    menu = read_menu(menu_text((STOP_WAIT, WAIT_AT, CREDITS)))
    assert menu is not None and [row.kind for row in menu.rows] == ["stop_wait", "wait", "credits"]
