"""Amendment A51: phone and terminal take turns in one attached Claude session.

The CLI's turn hooks tell the device where a turn stands; `auto` steers a
running turn at its next batch of tool calls, or at its `Stop` when it has
none left; `queue` keeps A19's one message per turn's end; and a message the
CLI absorbs or lets go goes in again until its own row shows.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, cast

import pytest

from rc_client import attachments
from rc_client.agents.base import Emit
from rc_client.agents.claude import transcripts
from rc_client.ids import block_uuid
from rc_client.sessions import shared as shared_module
from rc_client.sessions.attach import TurnHook
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.ptys import PtyLink
from rc_client.sessions.shared import (
    HELD_QUIET,
    INFLIGHT_TIMEOUT,
    PASSED_GRACE,
    PROMPT_GUARD,
    STEER_LEAD,
    STEER_TAIL,
    SharedState,
)
from tests.helpers import drained, tick
from tests.test_shared_control import SHOT, SHOT_SHOWN, FakeAttachment, Harness, message_folders


class BatchTerminal(PtyLink):
    """The terminal proxy of a CLI started with the `PostToolBatch` hook."""

    def __init__(self, pid: int = 4242) -> None:
        super().__init__(pid, cast(asyncio.StreamWriter, object()), tool_batch=True)


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    built.hub.ptys.add(BatchTerminal())
    yield built
    built.close()


class Later:
    """The clock the shared session reads, moved on by `ahead` seconds."""

    def __init__(self) -> None:
        self.ahead = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.ahead

    def time(self) -> float:
        return time.time() + self.ahead


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Later:
    later = Later()
    monkeypatch.setattr(shared_module, "time", later)
    return later


async def hook(
    harness: Harness, event: str, kind: str = "", message_id: str = "", deadline: float = 0.0
) -> Any:
    """One turn hook, as the attach server hands it to the hub."""
    return await harness.hub.attach_turn(TurnHook("sess-1", event, kind, message_id, deadline))


async def send(harness: Harness, request_id: str, text: str, **extra: Any) -> dict[str, Any]:
    params = {"id": request_id, "session_id": "sess-1", "text": text, **extra}
    result: dict[str, Any] = await harness.hub.send(params)
    return result


def injected(harness: Harness) -> list[str]:
    return [text for _, text in harness.attachment.injected]


def busy(state: SharedState) -> bool:
    """Read afresh each time: the state moves under the test's feet."""
    return state.busy


def kinds(harness: Harness) -> list[str]:
    """Every event the session published, in order, by kind."""
    return [
        frame["event"]["kind"] for frame in harness.frames if frame.get("type") == "session.event"
    ]


async def running_turn(harness: Harness) -> SessionEntry:
    """An attached session whose CLI runs the hooks and has a turn under way."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    return entry


# ------------------------------------------------------------------ busy


async def test_a_prompt_hook_starts_the_turn_before_the_transcript_shows_it(
    harness: Harness,
) -> None:
    entry = await running_turn(harness)
    assert entry.shared is not None and entry.shared.busy
    assert entry.session.state == "running"
    assert harness.events("turn_started")[-1]["trigger"] == "terminal"


async def test_a_slash_command_holds_injecting_back_without_showing_a_turn(
    harness: Harness,
) -> None:
    """A skill may start a turn, `/model` only a dialog: the transcript says which."""
    entry = await harness.attach()
    await hook(harness, "prompt", "command")
    assert entry.shared is not None and entry.shared.busy
    assert harness.events("turn_started") == []
    assert await send(harness, "r1", "go") == {"accepted": "steered"}

    assert entry.shared.command_at is not None
    entry.shared.command_at -= PROMPT_GUARD + 1
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["go"]


async def test_a_prompt_no_turn_followed_lapses_and_what_waited_goes_in(
    harness: Harness,
) -> None:
    """A turn the person stopped before `Stop` ran ends with the transcript (A32, A51)."""
    entry = await running_turn(harness)
    assert await send(harness, "r1", "later") == {"accepted": "steered"}
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == [], "the prompt is fresh: its turn has not reached the file"

    assert entry.shared is not None and entry.shared.prompted_at is not None
    entry.shared.prompted_at -= PROMPT_GUARD + 1
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["later"]
    assert harness.events("user_message")[-1]["delivery"] == "delivered"


async def test_subagents_at_work_never_hold_a_message_back(harness: Harness) -> None:
    """Working means all of it for the apps; the CLI's own turn decides injecting."""
    entry = await harness.attach()
    await tick(harness.hub, entry, running=False, working=True)
    assert entry.session.state == "running"
    assert await send(harness, "r1", "between their steps") == {"accepted": "sent"}
    assert injected(harness) == ["between their steps"]


async def test_a_cli_started_without_the_batch_hook_keeps_the_A19_hold(
    harness: Harness,
) -> None:
    """Too old for `PostToolBatch`, it got the settings without it: no steer, no `Stop` delivery."""
    harness.hub.ptys.clear()
    entry = await running_turn(harness)
    assert await send(harness, "r1", "later") == {"accepted": "queued", "queued_id": "r1"}
    await hook(harness, "stop")
    assert injected(harness) == [], "the transcript's turn end delivers, as before A51"
    await tick(harness.hub, entry, running=False)
    await drained(entry)
    assert injected(harness) == ["later"]


async def test_a_cli_without_the_turn_hooks_keeps_the_A19_hold(harness: Harness) -> None:
    entry = await harness.attach()
    await tick(harness.hub, entry, running=True)
    assert await send(harness, "r1", "later") == {"accepted": "queued", "queued_id": "r1"}
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["later"]


async def test_the_resume_prompt_never_steers(harness: Harness) -> None:
    """The device's own sentence after a usage limit waits its turn (A35)."""
    entry = await running_turn(harness)
    params = {"session_id": "sess-1", "text": "Continue.", "mode": "auto"}
    reply = await harness.hub.send(params, source="resume")
    assert reply["accepted"] == "queued"
    assert entry.shared is not None and entry.shared.steers == []


# -------------------------------------------------------------- steering


async def test_a_steer_reaches_the_model_at_the_next_batch_of_tool_calls(
    harness: Harness,
) -> None:
    entry = await running_turn(harness)
    assert await send(harness, "r1", "also check the tests") == {"accepted": "steered"}
    assert harness.events("user_message") == [], "no block until the model has it (A14)"
    assert harness.events("queue") == [], "a steer is no queue entry"

    reply = await hook(harness, "batch")
    assert injected(harness) == [], "nothing goes through the channel mid-turn"
    assert entry.shared is not None
    [message_id] = list(entry.shared.handed)
    assert reply["context"] == (
        f"{STEER_LEAD}\n"
        f'<channel source="rc" origin="app" message_id="{message_id}">\n'
        "also check the tests\n</channel>\n\n"
        f"{STEER_TAIL}"
    )
    assert reply["system_message"] == "SideCode · remote message: also check the tests"
    assert harness.events("user_message") == [], "drawn when the transcript shows it"

    # The transcript gives the handed-over context back, which confirms it.
    await harness.hub.shared.steer_delivered(entry, [message_id])
    [bubble] = harness.events("user_message")
    assert (bubble["block_id"], bubble["delivery"], bubble["source"]) == (
        block_uuid("r1"),
        "delivered",
        "remote",
    )
    assert entry.shared.handed == {}


def boundary_rows(context: str, version: str = "2.1.303") -> list[dict[str, Any]]:
    """What Claude Code writes at a tool boundary where the person had queued a prompt."""
    return [
        {"type": "queue-operation", "operation": "remove", "reason": "absorbed_mid_turn"},
        {
            "type": "attachment",
            "version": version,
            "attachment": {"type": "hook_success", "hookEvent": "PostToolBatch"},
        },
        {
            "type": "attachment",
            "version": version,
            "attachment": {
                "type": "hook_additional_context",
                "hookEvent": "PostToolBatch",
                "content": [context],
            },
        },
        {
            "type": "attachment",
            "uuid": "fold-1",
            "version": version,
            "attachment": {
                "type": "queued_command",
                "prompt": "and the docs",
                "commandMode": "prompt",
                "origin": {"kind": "human"},
            },
        },
        {"type": "attachment", "version": version, "attachment": {"type": "total_tokens_reminder"}},
    ]


def told(rows: list[dict[str, Any]]) -> list[Emit]:
    """What a reader makes of these rows, without the queue's comings and goings."""
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    return [
        emit
        for row in rows
        for emit in tailer.translate(row)
        if emit.kind != transcripts.QUEUE_MOVED
    ]


async def test_the_transcript_confirms_a_steer_after_what_the_boundary_folded_in(
    harness: Harness,
) -> None:
    """The model reads the person's folded prompt first, and so does the phone."""
    entry = await running_turn(harness)
    await send(harness, "r1", "also check the tests")
    reply = await hook(harness, "batch")
    emits = told(boundary_rows(reply["context"]))
    assert [emit.kind for emit in emits] == ["user_message", transcripts.STEER_DELIVERED]
    assert emits[0].fields == {"block_id": "fold-1", "text": "and the docs", "source": "terminal"}
    assert entry.shared is not None
    assert emits[1].fields == {"message_ids": list(entry.shared.handed)}


async def test_claude_code_2_1_291_reads_the_steer_before_what_it_folded_in(
    harness: Harness,
) -> None:
    await running_turn(harness)
    await send(harness, "r1", "also check the tests")
    reply = await hook(harness, "batch")
    emits = told(boundary_rows(reply["context"], version="2.1.291"))
    assert [emit.kind for emit in emits] == [transcripts.STEER_DELIVERED, "user_message"]


async def test_only_auto_steers_queue_and_interrupt_wait_their_turn(harness: Harness) -> None:
    entry = await running_turn(harness)
    assert await send(harness, "q", "queued", mode="queue") == {
        "accepted": "queued",
        "queued_id": "q",
    }
    assert await send(harness, "i", "interrupting", mode="interrupt") == {
        "accepted": "queued",
        "queued_id": "i",
    }
    assert await send(harness, "a", "steered", mode="auto") == {"accepted": "steered"}
    assert [item["id"] for item in entry.queue] == ["q", "i"]
    assert entry.shared is not None and [item["id"] for item in entry.shared.steers] == ["a"]


async def test_a_retried_steer_request_gets_the_same_answer_and_steers_once(
    harness: Harness,
) -> None:
    entry = await running_turn(harness)
    assert await send(harness, "r1", "once") == {"accepted": "steered"}
    assert await send(harness, "r1", "once") == {"accepted": "steered"}
    assert entry.shared is not None and len(entry.shared.steers) == 1


async def test_a_batch_with_nothing_steered_answers_empty(harness: Harness) -> None:
    await running_turn(harness)
    assert await hook(harness, "batch") == {}


async def test_a_batch_hands_over_every_steer_whatever_the_person_queued_between(
    harness: Harness,
) -> None:
    """Claude Code places the person's queued prompt by its own rule, so holding a
    later steer back would only delay it."""
    entry = await running_turn(harness)
    assert await send(harness, "phone-1", "first from the phone") == {"accepted": "steered"}
    await hook(harness, "prompt", "person")  # the person queues one in the terminal
    assert await send(harness, "phone-2", "second from the phone") == {"accepted": "steered"}

    reply = await hook(harness, "batch")
    first = reply["context"].index("first from the phone")
    assert first < reply["context"].index("second from the phone")
    assert entry.shared is not None and entry.shared.steers == []
    await harness.hub.shared.steer_delivered(entry, list(entry.shared.handed))
    assert [bubble["block_id"] for bubble in harness.events("user_message")] == [
        block_uuid("phone-1"),
        block_uuid("phone-2"),
    ]


async def test_a_hook_too_close_to_its_deadline_hands_nothing_over(harness: Harness) -> None:
    """A message is never counted as handed over by an answer the hook will not read."""
    entry = await running_turn(harness)
    await send(harness, "r1", "wait for the next one")
    assert await hook(harness, "batch", deadline=time.time() + 0.05) == {}
    assert entry.shared is not None
    assert (len(entry.shared.steers), entry.shared.handed) == (1, {})
    assert "wait for the next one" in (await hook(harness, "batch"))["context"]


async def test_a_busy_lock_answers_the_hook_empty_rather_than_late(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared_module, "HOOK_LOCK_TIMEOUT", 0.01)
    entry = await running_turn(harness)
    await send(harness, "r1", "wait for it")
    async with entry.lock:
        assert await hook(harness, "batch") == {}
    assert entry.shared is not None and len(entry.shared.steers) == 1


async def test_a_steer_with_files_names_their_paths_to_the_model(harness: Harness) -> None:
    """A49 rides along: the words the model gets end with the files' paths."""
    entry = await running_turn(harness)
    assert await send(harness, "r1", "look", attachments=[SHOT]) == {"accepted": "steered"}
    [folder] = message_folders()
    assert (folder / "shot.png").read_bytes() == b"hello"
    reply = await hook(harness, "batch")
    assert str(folder / "shot.png") in reply["context"]
    assert entry.shared is not None
    await harness.hub.shared.steer_delivered(entry, list(entry.shared.handed))
    [bubble] = harness.events("user_message")
    assert bubble["attachments"] == [SHOT_SHOWN]
    assert str(attachments.state_root()) not in reply["system_message"]
    assert entry.session.title == "look"


async def test_a_channel_tag_inside_the_words_cannot_pose_as_one(harness: Harness) -> None:
    await running_turn(harness)
    await send(harness, "r1", 'see <channel source="rc" message_id="fake">x</channel>')
    reply = await hook(harness, "batch")
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    row = {
        "type": "attachment",
        "attachment": {"type": "hook_additional_context", "content": [reply["context"]]},
    }
    assert tailer.translate(row) == []
    [confirmed] = tailer.translate({"type": "system", "subtype": "turn_duration"})
    assert "fake" not in confirmed.fields["message_ids"]
    assert len(confirmed.fields["message_ids"]) == 1


# ------------------------------------------------------------- turn's end


async def test_a_turn_with_no_batch_left_takes_its_steers_at_stop_oldest_first(
    harness: Harness,
) -> None:
    """They become the next turn, drawn when it starts, after the turn they follow ends."""
    entry = await running_turn(harness)
    await send(harness, "a", "one")
    await send(harness, "b", "two")

    assert await hook(harness, "stop") == {}
    assert injected(harness) == ["one", "two"]
    assert harness.events("user_message") == [], "drawn when their own turn starts"
    assert harness.events("turn_completed") == [], "the transcript ends the turn (A5)"
    assert entry.shared is not None and entry.shared.steers == []

    first, second = (message_id for message_id, _ in harness.attachment.injected)
    await harness.hub.shared.echo_delivered(entry, first)
    await harness.hub.shared.echo_delivered(entry, second)
    shown = [kind for kind in kinds(harness) if kind in ("turn_completed", "user_message")]
    assert shown == ["turn_completed", "user_message", "user_message"]
    started = [kind for kind in kinds(harness) if kind in ("turn_completed", "turn_started")]
    assert started == ["turn_started", "turn_completed", "turn_started"]
    assert harness.events("turn_started")[-1]["trigger"] == "remote"
    assert [bubble["block_id"] for bubble in harness.events("user_message")] == [
        block_uuid("a"),
        block_uuid("b"),
    ]


async def test_stop_leaves_the_end_of_the_turn_to_the_transcript(harness: Harness) -> None:
    """Somebody's own `Stop` hook may send the turn on; only the rows say it ended."""
    entry = await running_turn(harness)
    await hook(harness, "stop")
    assert entry.session.turn is not None
    assert harness.events("turn_completed") == []
    await tick(harness.hub, entry, running=False)
    assert len(harness.events("turn_completed")) == 1
    assert entry.session.state == "idle"


async def test_stop_with_nothing_to_deliver_does_not_wait_for_the_lock(harness: Harness) -> None:
    entry = await running_turn(harness)
    async with entry.lock:
        started = time.monotonic()
        assert await hook(harness, "stop") == {}
        assert time.monotonic() - started < shared_module.HOOK_LOCK_TIMEOUT / 2


async def test_stop_gives_the_up_next_queue_one_message_per_turn(harness: Harness) -> None:
    """A19 and A43 unchanged for `mode: "queue"`: the oldest goes at each turn's end."""
    entry = await running_turn(harness)
    first = await send(harness, "q1", "first queued", mode="queue")
    second = await send(harness, "q2", "second queued", mode="queue")
    assert (first["accepted"], second["accepted"]) == ("queued", "queued")

    await hook(harness, "stop")
    assert injected(harness) == ["first queued"]
    assert [item["id"] for item in entry.queue] == ["q2"]
    assert harness.events("queue")[-1]["pending"][0]["id"] == "q2"

    message_id, _ = harness.attachment.injected[-1]
    await harness.hub.shared.echo_delivered(entry, message_id)
    await hook(harness, "prompt", "ours", message_id)
    await hook(harness, "stop")
    assert injected(harness) == ["first queued", "second queued"]


async def test_a_queued_message_sent_before_the_steers_goes_in_with_them(
    harness: Harness,
) -> None:
    await running_turn(harness)
    await send(harness, "q1", "queued first", mode="queue")
    await send(harness, "s1", "steered after")
    await hook(harness, "stop")
    assert injected(harness) == ["queued first", "steered after"]


async def test_stop_waits_for_the_bridge_to_write_what_it_was_given(harness: Harness) -> None:
    landed: list[list[str]] = []

    async def record(message_ids: list[str], timeout: float) -> bool:
        landed.append(list(message_ids))
        return True

    harness.attachment.landed = record  # type: ignore[method-assign]
    await running_turn(harness)
    await send(harness, "a", "one")
    await hook(harness, "stop")
    assert landed == [[harness.attachment.injected[0][0]]]


# --------------------------------------------- what the CLI did not take


async def test_a_handed_steer_whose_row_never_comes_goes_in_again(
    harness: Harness, clock: Later
) -> None:
    """Once a later hook shows the CLI past that boundary, and never by time alone."""
    entry = await running_turn(harness)
    await send(harness, "r1", "did you get this")
    await hook(harness, "batch")
    assert entry.shared is not None
    [item] = entry.shared.handed.values()
    clock.ahead += 3600
    await tick(harness.hub, entry, running=False)
    assert list(entry.shared.handed.values()) == [item], "time alone loses nothing"

    await hook(harness, "stop")
    clock.ahead += PASSED_GRACE + 1
    await tick(harness.hub, entry, running=False)
    await drained(entry)
    assert injected(harness) == ["did you get this"]
    assert [bubble["delivery"] for bubble in harness.events("user_message")] == ["delivered"]
    assert item["retries"] == 1


async def test_a_steer_that_never_lands_fails_in_its_own_bubble(harness: Harness) -> None:
    entry = await running_turn(harness)
    await send(harness, "r1", "did you get this")
    await hook(harness, "batch")
    await hook(harness, "stop")
    assert entry.shared is not None
    [item] = entry.shared.handed.values()
    item["retries"] = shared_module.MAX_REDELIVERIES
    item["passed_at"] -= PASSED_GRACE + 1
    await tick(harness.hub, entry, running=False)
    [error] = harness.events("error")
    assert (error["block_id"], error["code"]) == (block_uuid("r1"), "conflict")
    assert injected(harness) == []


async def test_a_steer_written_at_stop_and_absorbed_goes_back_into_the_turn_that_went_on(
    harness: Harness,
) -> None:
    """Somebody's `Stop` hook sent the turn on: the message is handed over at its next
    batch, and that does not count against its re-sends."""
    entry = await running_turn(harness)
    await send(harness, "r1", "after the turn")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]

    await harness.hub.shared.echo_absorbed(entry, message_id)
    assert harness.events("user_message") == [], "it was never drawn, so it is not re-drawn"
    assert entry.shared is not None and [item["retries"] for item in entry.shared.steers] == [0]
    await hook(harness, "prompt", "person")
    assert "after the turn" in (await hook(harness, "batch"))["context"]


async def test_a_queued_message_written_at_stop_and_absorbed_also_goes_back_into_the_turn(
    harness: Harness,
) -> None:
    """The turn that went on had not ended, whatever the message's mode."""
    entry = await running_turn(harness)
    await send(harness, "q1", "up next", mode="queue")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]

    await harness.hub.shared.echo_absorbed(entry, message_id)
    assert entry.queue == []
    assert entry.shared is not None and [item["id"] for item in entry.shared.steers] == ["q1"]
    await hook(harness, "prompt", "person")
    assert "up next" in (await hook(harness, "batch"))["context"]


async def test_a_turn_going_on_and_on_gives_up_on_what_cannot_steer_after_ten(
    harness: Harness,
) -> None:
    """The resume prompt never steers: uncounted re-sends at each `Stop`, ten at most."""
    entry = await running_turn(harness)
    params = {"session_id": "sess-1", "text": "Continue.", "mode": "auto"}
    await harness.hub.send(params, source="resume")
    for _ in range(shared_module.MAX_FREE_REDELIVERIES + 1):
        await hook(harness, "stop")
        message_id, _ = harness.attachment.injected[-1]
        await harness.hub.shared.echo_absorbed(entry, message_id)
        await hook(harness, "prompt", "person")
    assert len(harness.attachment.injected) == shared_module.MAX_FREE_REDELIVERIES + 1
    [error] = harness.events("error")
    assert error["code"] == "conflict"
    assert entry.queue == []


def queue_row(operation: str, message_id: str, reason: str = "") -> list[Any]:
    """What a reader makes of one of the CLI's queue rows about our message."""
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    content = f'<channel source="rc" origin="app" message_id="{message_id}">\ndo it\n</channel>'
    row = {"type": "queue-operation", "operation": operation, "content": content}
    if reason:
        row["reason"] = reason
    told = tailer.translate(row)
    return [(emit.kind, emit.fields) for emit in told if emit.kind != transcripts.QUEUE_MOVED]


async def test_a_message_the_cli_removed_goes_in_again_and_counts(harness: Harness) -> None:
    """The CLI cleared its queue: losing the message is worse than sending it twice."""
    entry = await harness.attach()
    assert await send(harness, "r1", "do it") == {"accepted": "sent"}
    message_id, _ = harness.attachment.injected[0]
    assert queue_row("remove", message_id, "cleared_on_cancel") == [
        (transcripts.CHANNEL_DROPPED, {"message_id": message_id})
    ]
    assert queue_row("remove", message_id, "absorbed_mid_turn") == []

    await harness.hub.shared.echo_queued(entry, message_id)
    await harness.hub.shared.echo_dropped(entry, message_id)
    assert harness.events("user_message")[-1]["delivery"] == "absorbed"
    await tick(harness.hub, entry, running=False)
    await drained(entry)
    assert injected(harness) == ["do it", "do it"]
    assert entry.sent[harness.attachment.injected[-1][0]]["retries"] == 1


@pytest.mark.parametrize("seen_as", ["prompt", "row"])
async def test_a_removed_message_that_reaches_the_cli_after_all_is_not_sent_twice(
    harness: Harness, seen_as: str
) -> None:
    entry = await harness.attach()
    await send(harness, "r1", "do it")
    message_id, _ = harness.attachment.injected[0]
    await hook(harness, "prompt", "person")  # a turn runs, so the copy waits
    await harness.hub.shared.echo_dropped(entry, message_id)
    assert entry.shared is not None and [item["id"] for item in entry.shared.steers] == ["r1"]

    if seen_as == "prompt":
        await hook(harness, "prompt", "ours", message_id)
    else:
        await harness.hub.shared.echo_delivered(entry, message_id)
    assert entry.shared.steers == []
    assert harness.events("user_message")[-1]["delivery"] == "delivered"
    entry.shared.prompted_at = None
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["do it"]


async def test_a_message_pulled_back_into_the_input_is_left_to_the_person(
    harness: Harness,
) -> None:
    """Pulled back to be edited: the hold goes, nothing is sent again, the bubble stays."""
    entry = await harness.attach()
    await send(harness, "r1", "do it")
    message_id, _ = harness.attachment.injected[0]
    assert queue_row("popAll", message_id) == [
        (transcripts.CHANNEL_PULLED, {"message_id": message_id})
    ]
    await harness.hub.shared.echo_queued(entry, message_id)
    await harness.hub.shared.echo_pulled(entry, message_id)
    assert entry.shared is not None and not entry.shared.busy
    assert [bubble["delivery"] for bubble in harness.events("user_message")] == ["delivered"]
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["do it"]
    await harness.hub.shared.echo_delivered(entry, message_id)
    assert [bubble["delivery"] for bubble in harness.events("user_message")] == ["delivered"]


def queue_row_naming(operation: str, *message_ids: str, reason: str = "") -> dict[str, Any]:
    """One of the CLI's queue rows about several of our messages at once."""
    content = "\n".join(
        f'<channel source="rc" origin="app" message_id="{message_id}">\nwords\n</channel>'
        for message_id in message_ids
    )
    row = {"type": "queue-operation", "operation": operation, "content": content}
    if reason:
        row["reason"] = reason
    return row


@pytest.mark.parametrize(
    ("operation", "reason", "kind"),
    [
        ("enqueue", "", transcripts.CHANNEL_QUEUED),
        ("remove", "cleared_on_cancel", transcripts.CHANNEL_DROPPED),
        ("popAll", "", transcripts.CHANNEL_PULLED),
    ],
)
def test_a_queue_row_speaks_for_every_message_it_names(
    operation: str, reason: str, kind: str
) -> None:
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    told = tailer.translate(queue_row_naming(operation, "m-1", "m-2", reason=reason))
    assert [(emit.kind, emit.fields) for emit in told if emit.kind != transcripts.QUEUE_MOVED] == [
        (kind, {"message_id": "m-1"}),
        (kind, {"message_id": "m-2"}),
    ]


async def test_one_pull_back_row_naming_two_held_messages_releases_both(
    harness: Harness, clock: Later
) -> None:
    """Claude Code takes its whole queue back into the input box in one `popAll` row."""
    entry = await harness.attach()
    await send(harness, "r1", "first")
    first_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_queued(entry, first_id)
    clock.ahead += HELD_QUIET + 1
    await send(harness, "r2", "second")
    second_id, _ = harness.attachment.injected[1]
    await harness.hub.shared.echo_queued(entry, second_id)

    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    for emit in tailer.translate(queue_row_naming("popAll", first_id, second_id)):
        if emit.kind == transcripts.CHANNEL_PULLED:
            await harness.hub.shared.echo_pulled(entry, str(emit.fields["message_id"]))
    assert entry.shared is not None and entry.shared.held_by_cli == {}
    assert await send(harness, "r3", "third") == {"accepted": "sent"}
    assert harness.events("user_message")[-1]["text"] == "third", "nothing is held any more"


async def test_a_message_written_at_stop_goes_in_again_once_the_cli_went_past_it(
    harness: Harness, clock: Later
) -> None:
    """A later turn hook, with neither its row nor a word from the CLI's queue: lost."""
    entry = await running_turn(harness)
    await send(harness, "r1", "after the turn")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]

    await hook(harness, "stop")  # the turn went on and ended again, the message never seen
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["after the turn"], "the transcript may still be catching up"
    clock.ahead += PASSED_GRACE + 1
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["after the turn", "after the turn"]
    copy = entry.sent[harness.attachment.injected[-1][0]]
    assert copy["retries"] == 1
    await harness.hub.shared.echo_delivered(entry, message_id)
    assert [bubble["delivery"] for bubble in harness.events("user_message")] == ["delivered"]


async def test_the_prompt_that_is_the_message_is_no_step_past_it(harness: Harness) -> None:
    entry = await running_turn(harness)
    await send(harness, "r1", "after the turn")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]
    await hook(harness, "prompt", "ours", message_id)
    assert "passed_at" not in entry.sent[message_id]


async def test_a_message_written_at_stop_and_seen_late_withdraws_its_copy(
    harness: Harness, clock: Later
) -> None:
    entry = await running_turn(harness)
    await send(harness, "r1", "after the turn")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]
    await hook(harness, "prompt", "person")  # another turn: the copy waits as a steer
    clock.ahead += PASSED_GRACE + 1
    await tick(harness.hub, entry, running=True)
    assert entry.shared is not None and [item["id"] for item in entry.shared.steers] == ["r1"]

    await harness.hub.shared.echo_delivered(entry, message_id)
    assert entry.shared.steers == []
    assert await hook(harness, "batch") == {}
    assert injected(harness) == ["after the turn"]


@pytest.mark.parametrize("held_by_cli", [False, True])
async def test_a_slow_stop_hook_never_gets_a_message_sent_twice(
    harness: Harness, clock: Later, held_by_cli: bool
) -> None:
    """Somebody's `Stop` hook runs the test suite: no row and no hook for longer than any
    timeout. The wait ends, so what came after goes in, but nothing is taken for lost, and
    the bubbles come in the order the CLI takes the messages, which is the order sent."""
    entry = await running_turn(harness)
    await send(harness, "r1", "run the migrations")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]
    if held_by_cli:
        await harness.hub.shared.echo_queued(entry, message_id)
    assert await send(harness, "r2", "then seed the data") == {"accepted": "steered"}

    clock.ahead += 3600
    for _ in range(3):
        await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["run the migrations", "then seed the data"]
    assert entry.shared is not None and entry.shared.steers == [] and entry.queue == []
    assert harness.events("user_message") == [], "the CLI still holds the first one"

    later_id, _ = harness.attachment.injected[1]
    await harness.hub.shared.echo_delivered(entry, message_id)
    await harness.hub.shared.echo_delivered(entry, later_id)
    await tick(harness.hub, entry, running=True)
    assert injected(harness) == ["run the migrations", "then seed the data"]
    bubbles = [(bubble["text"], bubble["delivery"]) for bubble in harness.events("user_message")]
    assert bubbles == [
        ("run the migrations", "delivered"),
        ("then seed the data", "delivered"),
    ]


async def test_a_message_sent_behind_one_a_dialog_holds_is_drawn_when_its_row_shows(
    harness: Harness, clock: Later
) -> None:
    """The CLI queued the first behind a dialog; the second, sent once the wait ended,
    goes in after it, so its bubble waits for its own row. With nothing held, a message
    is drawn as it goes in."""
    entry = await harness.attach()
    assert await send(harness, "r1", "first") == {"accepted": "sent"}
    first_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_queued(entry, first_id)
    clock.ahead += HELD_QUIET + 1

    assert await send(harness, "r2", "second") == {"accepted": "sent"}
    second_id, _ = harness.attachment.injected[1]
    assert [bubble["text"] for bubble in harness.events("user_message")] == ["first"]
    await harness.hub.shared.echo_delivered(entry, first_id)
    await harness.hub.shared.echo_delivered(entry, second_id)
    assert [bubble["text"] for bubble in harness.events("user_message")] == ["first", "second"]

    await tick(harness.hub, entry, running=False)
    assert await send(harness, "r3", "third") == {"accepted": "sent"}
    assert [bubble["text"] for bubble in harness.events("user_message")][-1] == "third"


async def test_a_message_the_cli_holds_is_lost_only_by_what_the_cli_says(
    harness: Harness, clock: Later
) -> None:
    """Its enqueue row says the CLI has it: later hooks and time say nothing more."""
    entry = await running_turn(harness)
    await send(harness, "r1", "after the turn")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_queued(entry, message_id)
    await hook(harness, "prompt", "person")
    await hook(harness, "stop")
    clock.ahead += 3600
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["after the turn"]

    await harness.hub.shared.echo_dropped(entry, message_id)
    await tick(harness.hub, entry, running=False)
    assert injected(harness) == ["after the turn", "after the turn"]
    assert entry.sent[message_id]["retries"] == 1


@pytest.mark.parametrize("copy_row", [True, False])
async def test_a_copy_already_handed_over_is_drawn_once_and_never_sent_again(
    harness: Harness, clock: Later, copy_row: bool
) -> None:
    """The first one reached the CLI after all: the model reads the words twice, the
    risk left, but the apps see one delivery and nothing counts or goes in again."""
    entry = await running_turn(harness)
    await send(harness, "r1", "run the migrations")
    await hook(harness, "stop")
    message_id, _ = harness.attachment.injected[0]
    await hook(harness, "prompt", "person")
    clock.ahead += PASSED_GRACE + 1
    await tick(harness.hub, entry, running=True)
    assert entry.shared is not None
    [copy] = entry.shared.steers
    assert "run the migrations" in (await hook(harness, "batch"))["context"]

    await harness.hub.shared.echo_delivered(entry, message_id)
    if copy_row:
        await harness.hub.shared.steer_delivered(entry, [message_id])
    await harness.hub.shared.echo_absorbed(entry, message_id)
    await hook(harness, "stop")
    clock.ahead += PASSED_GRACE + 1
    await tick(harness.hub, entry, running=False)
    assert [bubble["delivery"] for bubble in harness.events("user_message")] == ["delivered"]
    assert copy["retries"] == 1
    assert entry.shared.handed == {} and entry.shared.steers == [] and entry.queue == []
    assert injected(harness) == ["run the migrations"]


async def test_an_absorbed_message_written_at_a_turns_end_is_drawn_delivered_again(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    assert await send(harness, "q1", "do it", mode="queue") == {"accepted": "sent"}
    message_id, _ = harness.attachment.injected[0]
    await hook(harness, "prompt", "person")  # the person's turn had begun
    await harness.hub.shared.echo_absorbed(entry, message_id)
    assert harness.events("user_message")[-1]["delivery"] == "absorbed"

    await hook(harness, "stop")
    assert injected(harness) == ["do it", "do it"]
    await harness.hub.shared.echo_delivered(entry, message_id)
    assert harness.events("user_message")[-1]["delivery"] == "delivered"


async def test_stop_asks_the_reader_for_the_turn_state_soon(harness: Harness) -> None:
    """A turn's last rows may have been read before its `Stop`: nothing else would ask."""
    settled: list[tuple[str, float]] = []

    class Reader:
        def read_soon(self) -> None:
            pass

        def settle_soon(self, session_id: str, delay: float) -> None:
            settled.append((session_id, delay))

    harness.hub.shared.reader = Reader()
    await running_turn(harness)
    await hook(harness, "stop")
    assert settled == [("sess-1", shared_module.STOP_SETTLE)]


async def test_a_hold_nothing_moves_for_stops_counting_as_busy(harness: Harness) -> None:
    """The CLI may let a message go without a word: the gate opens, nothing is re-sent."""
    entry = await harness.attach()
    await send(harness, "r1", "first")
    message_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_queued(entry, message_id)
    state = entry.shared
    assert state is not None
    state.inflight_at -= INFLIGHT_TIMEOUT + 1
    assert busy(state)

    state.queue_seen_at -= HELD_QUIET - 1
    await harness.hub.shared.queue_moved(entry)
    assert busy(state), "the transcript moved: still held"

    state.queue_seen_at -= HELD_QUIET + 1
    assert not busy(state)
    assert await send(harness, "r2", "second") == {"accepted": "sent"}
    assert injected(harness) == ["first", "second"]


async def test_our_prompt_taken_by_the_cli_is_no_longer_held(harness: Harness) -> None:
    entry = await harness.attach()
    await send(harness, "r1", "first")
    message_id, _ = harness.attachment.injected[0]
    await harness.hub.shared.echo_queued(entry, message_id)
    await hook(harness, "prompt", "ours", message_id)
    assert entry.shared is not None and entry.shared.held_by_cli == {}


async def test_an_injection_the_cli_queued_waits_as_long_as_the_cli_does(
    harness: Harness,
) -> None:
    """A dialog can hold the CLI's queue; the message is in flight until its row shows."""
    entry = await harness.attach()
    await send(harness, "r1", "first")
    message_id, _ = harness.attachment.injected[0]
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    tag = f'<channel source="rc" origin="app" message_id="{message_id}">'
    queued = [
        emit
        for emit in tailer.translate(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": f"{tag}\nfirst\n</channel>",
            }
        )
        if emit.kind != transcripts.QUEUE_MOVED
    ]
    assert [(emit.kind, emit.fields) for emit in queued] == [
        (transcripts.CHANNEL_QUEUED, {"message_id": message_id})
    ]
    await harness.hub.shared.echo_queued(entry, message_id)
    assert entry.shared is not None
    entry.shared.inflight_at -= INFLIGHT_TIMEOUT + 1
    assert entry.shared.busy
    assert (await send(harness, "r2", "second"))["accepted"] == "queued"

    await harness.hub.shared.echo_delivered(entry, message_id)
    assert not entry.shared.waiting


# ------------------------------------------------------- re-registration


async def test_a_bridge_registering_again_keeps_what_was_sent_and_steered(
    harness: Harness,
) -> None:
    entry = await running_turn(harness)
    await hook(harness, "stop")
    await tick(harness.hub, entry, running=False)
    assert await send(harness, "r1", "sent first") == {"accepted": "sent"}
    sent_id, _ = harness.attachment.injected[0]
    await hook(harness, "prompt", "ours", sent_id)
    assert await send(harness, "r2", "steered") == {"accepted": "steered"}

    replacement = FakeAttachment()
    await harness.hub.attach_registered(replacement)
    assert entry.shared is not None and entry.shared.attachment is replacement
    assert [item["text"] for item in entry.shared.steers] == ["steered"]
    assert sent_id in entry.sent

    # Absorbed after the new bridge came: still found, and sent again through it.
    await harness.hub.shared.echo_absorbed(entry, sent_id)
    await hook(harness, "stop")
    assert [text for _, text in replacement.injected] == ["sent first", "steered"]


async def test_a_message_handed_over_is_drawn_when_the_bridge_goes_first(
    harness: Harness,
) -> None:
    entry = await running_turn(harness)
    await send(harness, "r1", "handed")
    await hook(harness, "batch")
    await harness.hub.shared.closed(entry, harness.attachment, "terminal")
    [bubble] = harness.events("user_message")
    assert (bubble["block_id"], bubble["delivery"]) == (block_uuid("r1"), "delivered")
    assert entry.queue == []


async def test_steers_wait_with_the_held_messages_when_the_bridge_goes(
    harness: Harness,
) -> None:
    entry = await running_turn(harness)
    await send(harness, "r1", "steered")
    await harness.hub.shared.closed(entry, harness.attachment, "terminal")
    assert [item["text"] for item in entry.queue] == ["steered"]
    replacement = FakeAttachment()
    await harness.hub.attach_registered(replacement)
    await tick(harness.hub, entry, running=False)
    await drained(entry)
    assert [text for _, text in replacement.injected] == ["steered"]
