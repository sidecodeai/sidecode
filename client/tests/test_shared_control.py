"""Amendment A10: a session the device shares with a live terminal CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Choice
from rc_client.registry import Registry
from rc_client.sessions.attach import Attachment, HookQuestion
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.limits import TurnEnd
from rc_client.sessions.shared import SharedState

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
            capabilities=["takeover", "interrupt", "queue", "history"],
            attach="channel",
            attach_ready=True,
            shared_interrupt=False,
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
    await harness.hub.shared.tick(entry, running=True)
    states.append(entry.session.state)
    await harness.hub.shared.tick(entry, running=False)
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
    await harness.hub.shared.tick(entry, running=True)

    result = await harness.hub.send({"id": "req-2", "session_id": "sess-1", "text": "later"})
    assert result == {"accepted": "queued", "queued_id": "req-2"}
    assert harness.attachment.injected == []
    # Amendment A19: a held message is a queue entry and nothing more.
    assert harness.events("user_message") == []
    assert harness.events("queue")[-1]["pending"] == [
        {"id": "req-2", "text": "later", "ts": entry.queue[0]["ts"]}
    ]

    await harness.hub.shared.tick(entry, running=False)
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
    await harness.hub.shared.tick(entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["one"]

    message_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_delivered(entry, message_id)
    await harness.hub.shared.tick(entry, running=False)
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


async def test_an_absorbed_injection_is_marked_and_re_sent_once(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.send({"id": "req-3", "session_id": "sess-1", "text": "do it"})
    message_id, _ = harness.attachment.injected[0]

    await harness.hub.shared.echo_absorbed(entry, message_id)
    bubbles = harness.events("user_message")
    assert bubbles[-1]["delivery"] == "absorbed"
    assert bubbles[-1]["block_id"] == bubbles[0]["block_id"]
    assert [item["text"] for item in entry.queue] == ["do it"]

    await harness.hub.shared.tick(entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["do it", "do it"]
    assert harness.events("user_message")[-1]["delivery"] == "delivered"

    # Absorbed a second time: the device stops re-sending and says so. The CLI
    # read the message both times, so the bubble ends as delivered rather than
    # on a promise of a re-send that will not come (A34).
    second_id, _ = harness.attachment.injected[1]
    await harness.hub.shared.echo_absorbed(entry, second_id)
    assert entry.queue == []
    assert harness.events("notice")[-1]["level"] == "warn"
    last = harness.events("user_message")[-1]
    assert last["delivery"] == "delivered"
    assert last["block_id"] == bubbles[0]["block_id"]


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
    await harness.hub.shared.tick(entry, running=True)
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

    with pytest.raises(RcError) as raised:
        await harness.hub.send(
            {
                "session_id": "sess-1",
                "text": "look",
                "attachments": [{"name": "a.png", "mime": "image/png", "data_base64": "aGk="}],
            }
        )
    assert raised.value.code == "unsupported"


async def test_the_title_can_still_be_changed_on_a_shared_session(harness: Harness) -> None:
    await harness.attach()
    result = await harness.hub.set_options({"session_id": "sess-1", "title": "Pairing codes"})
    assert result["session"]["title"] == "Pairing codes"


async def test_a_pending_message_can_be_removed_from_the_queue(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
    await harness.hub.send({"id": "req-5", "session_id": "sess-1", "text": "never mind"})
    await harness.hub.queue_remove({"session_id": "sess-1", "queued_id": "req-5"})
    await harness.hub.shared.tick(entry, running=False)
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
    await harness.hub.shared.tick(entry, running=False)
    turns.append(entry.session.turn)
    assert [turn is not None for turn in turns] == [True, False]
    assert harness.events("turn_completed")[-1]["stop_reason"] == "completed"


async def test_a_turn_the_terminal_started_is_labelled_as_such(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
    assert harness.events("turn_started")[-1]["trigger"] == "terminal"
    await harness.hub.shared.tick(entry, running=False)
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

    await harness.hub.shared.tick(entry, reader.busy, reader.turn_trigger)
    assert harness.events("turn_started")[-1]["trigger"] == "agent"

    await harness.hub.shared.tick(entry, running=False)
    assert harness.events("turn_completed")[-1]["stop_reason"] == "completed"


async def test_a_turn_the_person_interrupted_is_closed_as_interrupted(harness: Harness) -> None:
    """Amendment A32: the marker ends the turn it found and starts none of its own."""
    entry = await harness.attach()
    reader = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    reader.translate(
        {"type": "user", "uuid": "row-typed", "message": {"role": "user", "content": "go"}}
    )
    await harness.hub.shared.tick(
        entry, reader.busy, reader.turn_trigger, TurnEnd(reader.stop_reason, reader.limit)
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
    await harness.hub.shared.tick(
        entry, reader.busy, reader.turn_trigger, TurnEnd(reader.stop_reason, reader.limit)
    )
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    assert len(harness.events("turn_started")) == starts
    assert entry.session.turn is None


async def test_a_turn_still_open_when_the_bridge_leaves_is_closed_as_stopped(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
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
    await harness.hub.shared.tick(entry, running=True)
    result = await harness.hub.send({"id": SEND_REQUEST, "session_id": "sess-1", "text": "later"})

    assert result == {"accepted": "queued", "queued_id": SEND_REQUEST}
    assert harness.events("queue")[-1]["pending"][0]["id"] == SEND_REQUEST
    await harness.hub.shared.tick(entry, running=False)
    bubbles = harness.events("user_message")
    assert [bubble["block_id"] for bubble in bubbles] == [SEND_REQUEST]
    assert [bubble["delivery"] for bubble in bubbles] == ["delivered"]


# ------------------------------------------- A19: a held message is a queue entry


async def test_a_held_message_is_issued_after_the_turn_it_waited_for(
    harness: Harness,
) -> None:
    """Its `first_seq` comes from the injection, so it lands where the terminal shows it."""
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
    await harness.hub.send({"id": "req-19", "session_id": "sess-1", "text": "later"})
    await entry.channel.emit("assistant_text", block_id="msg-1", text="working", done=True)

    await harness.hub.shared.tick(entry, running=False)
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
    await harness.hub.shared.tick(entry, running=tailer.busy)
    await harness.hub.send({"id": "req-34", "session_id": "sess-1", "text": "ping"})
    assert harness.attachment.injected == []

    tailer.translate(assistant_row([{"type": "text", "text": "Reading it now."}], "tool_use"))
    await harness.hub.shared.tick(entry, running=tailer.busy)
    assert harness.attachment.injected == [], "the turn had only paused to call a tool"

    tailer.translate(assistant_row([{"type": "text", "text": "Done."}], "end_turn"))
    await harness.hub.shared.tick(entry, running=tailer.busy)
    assert [text for _, text in harness.attachment.injected] == ["ping"]


async def test_a_message_held_across_a_detachment_is_never_shown_twice(
    harness: Harness,
) -> None:
    """No block was published while it waited, so the resume path publishes the first."""
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
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
    await harness.hub.shared.tick(entry, running=True)
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
    await harness.hub.shared.tick(entry, running=False)
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
