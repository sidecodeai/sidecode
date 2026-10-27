"""A session with a subagent at work is shown as working, whatever else the CLI says.

A CLI that runs the device's hooks says when each subagent starts and ends
(`SubagentStart`, `SubagentStop`, and `StopFailure` for one an API error ends),
so the session stays `running` for the apps for as long as one is, with the main
turn over, the `Stop` payload silent about it and the transcript no help. A
question or an approval still outranks it, and a turn the person interrupted or
the usage limit ended takes its subagents with it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.channel import turnhook
from rc_client.sessions import attach as attach_module
from rc_client.sessions.agents_at_work import (
    AGENT_HANDOFF,
    AGENT_SILENCE,
    MAX_AGENTS,
    WAIT_CREDIT,
    AgentsAtWork,
)
from rc_client.sessions.attach import TurnHook
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.limits import LimitStop, TurnEnd
from rc_client.sessions.shared import PROMPT_GUARD, SharedControl
from tests.helpers import tick

from .test_claude_subagents import MirrorHarness, assistant, user_row
from .test_shared_control import FakeAttachment, FakeHookQuestion
from .test_typist import Harness

LIMIT = LimitStop(window_minutes=300, resets_at_ms=1_800_000_000_000)


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


@pytest.fixture
async def mirrored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    built = MirrorHarness(tmp_path, monkeypatch)
    yield built
    await built.hub.close()
    built.close()


class Reader:
    """Stands in for the transcript reader, and remembers when a second look was asked for."""

    def __init__(self) -> None:
        self.looks: list[tuple[str, float]] = []

    def read_soon(self) -> None:
        return None

    def settle_soon(self, session_id: str, delay: float) -> None:
        self.looks.append((session_id, delay))


def shown(entry: SessionEntry) -> str:
    """The state the apps are told, read afresh each time: it moves under the test's feet."""
    return entry.session.state


async def hook(
    harness: Harness,
    event: str,
    kind: str = "",
    *,
    background: int = 0,
    listed: int | None = None,
    agent_id: str = "",
) -> None:
    """One turn hook, as the attach server hands it to the hub."""
    await harness.hub.attach_turn(
        TurnHook(
            "sess-1", event, kind, background=background, listed_agents=listed, agent_id=agent_id
        )
    )


async def start(harness: Harness, agent_id: str) -> None:
    await hook(harness, "agent_start", agent_id=agent_id)


async def end(harness: Harness, agent_id: str) -> None:
    await hook(harness, "agent_stop", agent_id=agent_id)


def at_work(entry: SessionEntry) -> AgentsAtWork:
    assert entry.shared is not None
    return entry.shared.agents


def go_quiet(entry: SessionEntry, agent_id: str, seconds: float) -> None:
    """`seconds` more have gone by since the subagent last showed a sign of life."""
    at_work(entry).seen[agent_id].alive -= seconds


def let_time_pass(entry: SessionEntry, seconds: float) -> None:
    """`seconds` go by: every stamp on the monotonic clock the state keeps moves back with them."""
    state = entry.shared
    assert state is not None
    if state.prompted_at is not None:
        state.prompted_at -= seconds
    for agent in state.agents.seen.values():
        agent.began -= seconds
        agent.alive -= seconds


def let_the_handoff_pass(entry: SessionEntry) -> None:
    assert entry.shared is not None
    stopped = entry.shared.agents.stopped_at
    assert stopped is not None
    entry.shared.agents.stopped_at = stopped - AGENT_HANDOFF - 1


# ------------------------------------------------------------------- the rule


async def test_a_subagent_starting_in_an_idle_session_shows_it_working(harness: Harness) -> None:
    entry = await harness.attach()
    assert shown(entry) == "idle"
    await start(harness, "a1")
    assert shown(entry) == "running"
    assert len(harness.events("turn_started")) == 1
    state = entry.shared
    assert state is not None and state.shown_busy
    assert not state.busy, "the CLI's own turn is what injecting waits for, and it is not one"


async def test_a_turn_over_with_a_subagent_at_work_leaves_the_session_running(
    harness: Harness,
) -> None:
    """The main agent ends its turn to wait for its subagents."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await start(harness, "a1")
    await start(harness, "a2")
    # A Stop that says nothing about them, as a Claude Code with no task list does.
    await hook(harness, "stop")
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"
    assert harness.events("turn_completed") == []
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"


async def test_the_session_stays_running_until_the_last_subagent_has_ended(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    await start(harness, "a2")
    await end(harness, "a1")
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"
    await end(harness, "a2")
    assert shown(entry) == "running", "a moment is left for the prompt the CLI wakes with"
    let_the_handoff_pass(entry)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_started")) == 1
    assert len(harness.events("turn_completed")) == 1


async def test_a_subagent_resumed_after_it_ended_is_at_work_again(harness: Harness) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    await end(harness, "a1")
    let_the_handoff_pass(entry)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    await start(harness, "a1")
    assert shown(entry) == "running"
    assert len(harness.events("turn_started")) == 2


async def test_the_prompt_the_cli_wakes_with_takes_over_without_ending_the_turn(
    harness: Harness,
) -> None:
    """One turn from the person's prompt to the last subagent's report, as the apps see it."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await start(harness, "a1")
    await hook(harness, "stop", background=1, listed=1)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"

    # The subagent ends, and the CLI wakes with its report a moment later.
    await end(harness, "a1")
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"
    await hook(harness, "prompt", "agent")
    assert shown(entry) == "running"
    await hook(harness, "stop", background=0, listed=0)
    await tick(harness.hub, entry, running=False)

    assert shown(entry) == "idle"
    assert len(harness.events("turn_started")) == 1
    assert len(harness.events("turn_completed")) == 1


async def test_a_subagent_ending_leaves_the_session_to_be_looked_at_again(
    harness: Harness,
) -> None:
    """Nobody may tell the apps that it is idle but the second look, so it is asked for."""
    await harness.attach()
    reader = Reader()
    harness.hub.shared.reader = reader
    await start(harness, "a1")
    assert reader.looks == []
    await end(harness, "a1")
    assert reader.looks == [("sess-1", AGENT_HANDOFF)]


async def test_the_mirror_reading_the_turn_over_leaves_what_the_hooks_say_runs(
    mirrored: MirrorHarness,
) -> None:
    """No file of the subagent's anywhere, and the parent's turn ended: the hooks still say so."""
    entry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    await mirrored.mirror.tail_once()
    assert entry.session.state == "running"

    await mirrored.hub.attach_turn(TurnHook("sess-1", "agent_start", agent_id="a1"))
    mirrored.write([assistant("end_turn")])
    await mirrored.mirror.tail_once()
    await mirrored.mirror.scan_once()
    assert entry.session.state == "running"
    assert mirrored.events("turn_completed") == []

    await mirrored.hub.attach_turn(TurnHook("sess-1", "agent_stop", agent_id="a1"))
    let_the_handoff_pass(entry)
    await mirrored.mirror.tail_once()
    await mirrored.mirror.scan_once()
    assert entry.session.state == "idle"
    assert len(mirrored.events("turn_started")) == 1
    assert len(mirrored.events("turn_completed")) == 1


async def test_the_mirror_reading_an_escape_clears_the_subagents_it_aborted(
    mirrored: MirrorHarness,
) -> None:
    """The rows as Claude Code files them: no `Stop`, no `SubagentStop`, only the marker."""
    entry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    await mirrored.mirror.tail_once()
    mirrored.write([assistant("tool_use")])
    await mirrored.hub.attach_turn(TurnHook("sess-1", "agent_start", agent_id="a1"))
    await mirrored.mirror.tail_once()
    assert entry.session.state == "running"

    mirrored.write([user_row("[Request interrupted by user for tool use]", "u2")])
    await mirrored.mirror.tail_once()
    assert at_work(entry).seen == {}
    assert entry.session.state == "idle"
    assert mirrored.events("turn_completed")[-1]["stop_reason"] == "interrupted"


async def test_the_mirror_reading_an_escape_ends_the_subagent_whose_transcript_says_so(
    mirrored: MirrorHarness,
) -> None:
    """The transcript watch counts it too: no half an hour's hold on a subagent that was aborted."""
    entry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    await mirrored.mirror.tail_once()
    mirrored.write([assistant("tool_use")])
    mirrored.subagent("agent-a1", [assistant("tool_use")])
    await mirrored.hub.attach_turn(TurnHook("sess-1", "agent_start", agent_id="a1"))
    await mirrored.mirror.tail_once()
    assert entry.session.state == "running"

    marker = "[Request interrupted by user for tool use]"
    mirrored.subagent("agent-a1", [user_row(marker, "s1")])
    mirrored.write([user_row(marker, "u2")])
    await mirrored.mirror.tail_once()
    assert at_work(entry).seen == {}
    assert entry.session.state == "idle"
    assert mirrored.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    await mirrored.mirror.tail_once()
    assert entry.session.state == "idle", "nor does the next read bring it back"


async def test_a_failure_in_reporting_the_subagents_costs_the_pass_neither_rows_nor_state(
    mirrored: MirrorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows were read for good the moment they were: nothing that follows may lose them."""
    entry = await mirrored.attach()
    await mirrored.hub.attach_turn(TurnHook("sess-1", "agent_start", agent_id="a1"))
    go_quiet(entry, "a1", AGENT_SILENCE + 60)
    await mirrored.mirror.scan_once()
    mirrored.subagent("agent-a1", [assistant("tool_use")])  # it writes again: a revival is due
    mirrored.write([user_row("a message the person typed", "u-new")])

    async def broken(self: SharedControl, entry: SessionEntry, written: Any) -> None:
        raise RuntimeError("the registry was busy")

    monkeypatch.setattr(SharedControl, "agents_alive", broken)
    await mirrored.mirror.tail_once()
    texts = [event.get("text") for event in mirrored.events("user_message")]
    assert "a message the person typed" in texts
    assert entry.session.state == "running", "and the turn it began is reported"


# ------------------------------------------------------- what outranks, and what not


async def test_a_pending_approval_outranks_a_subagent_at_work(harness: Harness) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    assert shown(entry) == "running"
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r1", "tool_name": "Bash", "input_preview": '{"command": "make"}'},
    )
    assert shown(entry) == "needs_approval"
    request_id = harness.events("approval")[-1]["request_id"]
    await harness.hub.approve(
        {"session_id": "sess-1", "request_id": request_id, "option_id": "allow"}
    )
    assert shown(entry) == "running", "back to the work that was under way"


async def test_a_pending_question_outranks_a_subagent_at_work(harness: Harness) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    answering = FakeHookQuestion()
    await harness.hub.attach_question(answering)
    assert shown(entry) == "needs_input"
    await end(harness, "a1")
    let_the_handoff_pass(entry)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "needs_input", "nothing running does not answer it"
    await harness.hub.attach_question_closed(answering)
    assert shown(entry) == "idle"


async def test_a_subagent_at_work_never_holds_a_message_back(harness: Harness) -> None:
    """A51: the CLI takes a message between its subagents' steps; the apps still see them work."""
    entry = await harness.attach()
    await start(harness, "a1")
    sent = await harness.hub.send({"id": "r1", "session_id": "sess-1", "text": "and then?"})
    assert sent == {"accepted": "sent"}
    assert [text for _, text in harness.attachment.injected] == ["and then?"]
    assert entry.queue == []
    assert shown(entry) == "running"


# ------------------------------------------------------------------ the bounds


async def test_a_stop_whose_list_holds_no_agent_clears_an_end_that_never_came(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await start(harness, "a1")
    await hook(harness, "stop", background=0, listed=0)
    await tick(harness.hub, entry, running=False)
    assert at_work(entry).seen == {}
    assert shown(entry) == "idle"
    assert len(harness.events("turn_completed")) == 1


@pytest.mark.parametrize("listed", [None, 1, 2])
async def test_a_stop_that_lists_agents_or_no_list_clears_nothing(
    harness: Harness, listed: int | None
) -> None:
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await start(harness, "a1")
    await hook(harness, "stop", listed=listed)
    await tick(harness.hub, entry, running=False)
    assert list(at_work(entry).seen) == ["a1"]
    assert shown(entry) == "running"


async def test_a_subagent_whose_transcript_keeps_being_written_is_never_let_go(
    harness: Harness,
) -> None:
    """The cap is half an hour of silence, not half an hour of work: a long job stays green."""
    entry = await harness.attach()
    await start(harness, "a1")
    for _ in range(5):
        go_quiet(entry, "a1", AGENT_SILENCE - 60)
        await tick(harness.hub, entry, running=False)
        assert shown(entry) == "running"
        await harness.hub.shared.agents_alive(entry, {"a1": time.time()})
    go_quiet(entry, "a1", AGENT_SILENCE + 1)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle", "half an hour after its last word it goes"
    assert len(harness.events("turn_completed")) == 1


async def test_a_sign_of_life_from_a_subagent_never_heard_starting_is_none_of_ours(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.shared.agents_alive(entry, {"ghost": time.time()})
    assert at_work(entry).seen == {}
    assert shown(entry) == "idle"
    assert harness.events("turn_started") == []


async def test_a_subagent_transcript_still_being_written_keeps_the_hold(
    mirrored: MirrorHarness,
) -> None:
    """Whatever its last row says, a write is a sign of life; it ends a hold that was lost."""
    entry = await mirrored.attach()
    await mirrored.hub.attach_turn(TurnHook("sess-1", "agent_start", agent_id="a1"))
    path = mirrored.subagent("agent-a1", [assistant("end_turn")])
    go_quiet(entry, "a1", AGENT_SILENCE + 10)
    await mirrored.mirror.tail_once()
    assert entry.session.state == "running", "the write just now refreshed it"
    assert at_work(entry).seen["a1"].alive > time.monotonic() - 5

    # Nothing written for half an hour, and the hold lapses all the same.
    old = time.time() - AGENT_SILENCE - 60
    os.utime(path, (old, old))
    go_quiet(entry, "a1", AGENT_SILENCE + 10)
    await mirrored.mirror.tail_once()
    await mirrored.mirror.scan_once()
    assert entry.session.state == "idle"


async def test_the_wait_for_the_persons_answer_is_no_silence_of_the_subagent(
    harness: Harness,
) -> None:
    """Blocked on a prompt for forty minutes, it writes nothing; its answer brings it back."""
    entry = await harness.attach()
    await start(harness, "a1")
    await start(harness, "let-go-before")
    go_quiet(entry, "let-go-before", AGENT_SILENCE + 600)
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r1", "tool_name": "Bash", "input_preview": '{"command": "make all"}'},
    )
    [raised] = [approval.raised for approval in entry.shared.approvals.values()]  # type: ignore[union-attr]
    assert shown(entry) == "needs_approval"
    let_time_pass(entry, 2400.0)
    approval = next(iter(entry.shared.approvals.values()))  # type: ignore[union-attr]
    approval.raised = raised - 2400.0
    assert not at_work(entry).running

    request_id = harness.events("approval")[-1]["request_id"]
    await harness.hub.approve(
        {"session_id": "sess-1", "request_id": request_id, "option_id": "allow"}
    )
    assert shown(entry) == "running", "it is at work again from this moment"
    assert at_work(entry).seen["a1"].at_work(time.monotonic())
    assert not at_work(entry).seen["let-go-before"].at_work(time.monotonic()), "and only it"


async def test_an_answer_to_a_card_hours_old_revives_no_dead_agent(harness: Harness) -> None:
    """Its end was lost, and its card, answered in the terminal long ago, is still on the phone."""
    entry = await harness.attach()
    await start(harness, "dead")
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r1", "tool_name": "Bash", "input_preview": '{"command": "make all"}'},
    )
    hours = 3 * 3600.0
    let_time_pass(entry, hours)
    approval = next(iter(entry.shared.approvals.values()))  # type: ignore[union-attr]
    approval.raised -= hours
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "needs_approval"

    request_id = harness.events("approval")[-1]["request_id"]
    await harness.hub.approve(
        {"session_id": "sess-1", "request_id": request_id, "option_id": "allow"}
    )
    assert not at_work(entry).running
    assert shown(entry) == "idle"


async def test_a_question_answered_ends_the_subagents_wait_as_well(harness: Harness) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    answering = FakeHookQuestion()
    await harness.hub.attach_question(answering)
    state = entry.shared
    assert state is not None and state.question is not None
    let_time_pass(entry, 2400.0)
    state.question.raised -= 2400.0
    assert not at_work(entry).running

    request_id = harness.events("question")[-1]["request_id"]
    await harness.hub.answer(
        {"session_id": "sess-1", "request_id": request_id, "answers": {"q0": ["o0"]}}
    )
    assert shown(entry) == "running"


async def test_a_sign_of_life_after_the_cap_shows_the_session_working_at_once(
    harness: Harness,
) -> None:
    """Let go for its silence, it writes its transcript: that is all that says so, and at once."""
    entry = await harness.attach()
    await start(harness, "a1")
    go_quiet(entry, "a1", AGENT_SILENCE + 60)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_started")) == 1

    await harness.hub.shared.agents_alive(entry, {"a1": time.time()})
    assert shown(entry) == "running"
    assert len(harness.events("turn_started")) == 2
    await harness.hub.shared.agents_alive(entry, {"a1": time.time()})
    assert len(harness.events("turn_started")) == 2, "a second sign changes nothing"


async def test_the_late_end_of_a_subagent_let_go_starts_no_phantom_turn(harness: Harness) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    go_quiet(entry, "a1", AGENT_SILENCE + 60)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert (len(harness.events("turn_started")), len(harness.events("turn_completed"))) == (1, 1)

    await end(harness, "a1")  # it was only slow, and now it really ends
    assert shown(entry) == "idle"
    assert at_work(entry).stopped_at is None
    assert (len(harness.events("turn_started")), len(harness.events("turn_completed"))) == (1, 1)


async def test_a_subagent_nothing_has_ended_for_half_an_hour_is_let_go(harness: Harness) -> None:
    """A hook that never came must not hold a session working for good."""
    entry = await harness.attach()
    await start(harness, "a1")
    assert AGENT_SILENCE == 1800.0
    go_quiet(entry, "a1", AGENT_SILENCE - 5)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"
    go_quiet(entry, "a1", 10)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_completed")) == 1


async def test_an_end_for_an_agent_never_heard_starting_does_not_end_a_turn_early(
    harness: Harness,
) -> None:
    """Claude Code's own agents end with a `SubagentStop` after every turn, and the apps hear
    this turn end from the transcript, which `Stop` leaves a moment to be read (A32, A35)."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await hook(harness, "stop", listed=0)
    await end(harness, "prompt-suggestion")
    assert shown(entry) == "running"
    assert harness.events("turn_completed") == []
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_completed")) == 1


async def test_an_end_for_an_agent_never_heard_starting_changes_nothing(
    harness: Harness,
) -> None:
    """Claude Code's own agents and ones that began before the daemon did."""
    entry = await harness.attach()
    reader = Reader()
    harness.hub.shared.reader = reader
    await end(harness, "ghost")
    assert shown(entry) == "idle"
    assert harness.events("turn_started") == []
    assert reader.looks == []
    assert at_work(entry).stopped_at is None
    # Nor does it end the handoff of the one that was heard.
    await start(harness, "a1")
    await end(harness, "ghost")
    assert shown(entry) == "running"


async def test_stop_from_an_app_lets_the_subagents_go(harness: Harness) -> None:
    """The CLI is idle and Escape stops none of them: Stop means "I am done waiting"."""
    entry = await harness.attach()
    await start(harness, "a1")
    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert at_work(entry).seen == {}
    assert shown(entry) == "idle"
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"


async def test_a_cli_that_goes_away_takes_its_subagents_with_it(harness: Harness) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    await harness.hub.shared.closed(entry, harness.attachment, "none")
    assert entry.shared is None
    assert shown(entry) == "idle"
    await harness.hub.attach_registered(FakeAttachment())
    attached = harness.hub.entry("sess-1")
    assert at_work(attached).seen == {}
    assert shown(attached) == "idle"


async def test_a_subagent_event_for_a_session_nothing_is_attached_to_is_ignored(
    harness: Harness,
) -> None:
    await hook(harness, "agent_start", agent_id="a1")
    assert harness.events("turn_started") == []


async def test_a_turn_the_person_interrupts_takes_the_subagents_it_waited_for_with_it(
    harness: Harness,
) -> None:
    """Escape aborts a subagent without a `SubagentStop`, and the turn runs no `Stop` either."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await start(harness, "a1")
    await tick(harness.hub, entry, running=True)
    state = entry.shared
    assert state is not None and state.prompted_at is not None
    let_time_pass(entry, PROMPT_GUARD + 1)
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert at_work(entry).seen == {}
    assert shown(entry) == "idle"
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"


async def test_an_escape_leaves_the_background_subagent_a_finished_batch_began(
    harness: Harness,
) -> None:
    """It has an abort signal of its own: only the subagent the turn waits for is aborted."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await start(harness, "background")
    at_work(entry).seen["background"].began -= 1.0
    await hook(harness, "batch")  # the batch that launched it has finished
    await start(harness, "foreground")
    await tick(harness.hub, entry, running=True)

    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert list(at_work(entry).seen) == ["background"]
    state = entry.shared
    assert state is not None and state.prompted_at is not None
    let_time_pass(entry, PROMPT_GUARD + 1)
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert shown(entry) == "running", "the background subagent still holds the session"
    assert harness.events("turn_completed") == []

    await end(harness, "background")
    let_the_handoff_pass(entry)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"
    assert len(harness.events("turn_started")) == 1
    assert len(harness.events("turn_completed")) == 1


async def test_an_interrupted_later_turn_leaves_the_background_subagent_of_an_earlier_one(
    harness: Harness,
) -> None:
    """The second turn's prompt wipes the `Stop` count; its Escape does not wipe the subagent."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await start(harness, "background")
    at_work(entry).seen["background"].began -= 1.0
    await hook(harness, "stop", background=1, listed=1)
    await tick(harness.hub, entry, running=False)

    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    state = entry.shared
    assert state is not None and state.prompted_at is not None
    let_time_pass(entry, PROMPT_GUARD + 1)
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert list(at_work(entry).seen) == ["background"]
    assert shown(entry) == "running"

    await end(harness, "background")  # and it really does end, later
    let_the_handoff_pass(entry)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "idle"


async def test_an_escape_read_in_the_pass_that_first_saw_the_turn_takes_its_subagents(
    harness: Harness,
) -> None:
    """The prompt's row and the Escape's are read together: no read saw the turn running."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await start(harness, "a1")
    state = entry.shared
    assert state is not None and not state.running and state.prompted_at is not None
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert at_work(entry).seen == {}


async def test_an_escape_in_a_turn_no_hook_announced_takes_every_subagent(
    harness: Harness,
) -> None:
    """With no prompt or batch heard there is nothing to tell the foreground ones by."""
    entry = await harness.attach()
    await start(harness, "a1")
    await start(harness, "a2")
    await tick(harness.hub, entry, running=True)
    state = entry.shared
    assert state is not None and state.prompted_at is None
    await tick(harness.hub, entry, running=False, end=TurnEnd("interrupted"))
    assert at_work(entry).seen == {}
    assert shown(entry) == "idle"


async def test_the_usage_limit_ending_a_turn_takes_its_subagents_with_it(
    harness: Harness,
) -> None:
    """Every request is refused, the subagents' too; A35 hangs the resume on this turn's end."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await start(harness, "a1")
    state = entry.shared
    assert state is not None and state.prompted_at is not None
    let_time_pass(entry, PROMPT_GUARD + 1)
    await tick(harness.hub, entry, running=False, end=TurnEnd("error", LIMIT))
    assert shown(entry) == "idle"
    ended = harness.events("turn_completed")[-1]
    assert ended["stop_reason"] == "error" and ended["limit"]["window_minutes"] == 300


async def test_a_refused_prompt_read_with_its_refusal_in_one_pass_takes_them_too(
    harness: Harness,
) -> None:
    """No read saw that turn running, yet the hooks heard its prompt and no `Stop` followed."""
    entry = await harness.attach()
    await start(harness, "background")
    await hook(harness, "prompt", "person")
    state = entry.shared
    assert state is not None and state.prompted_at is not None and not state.running
    let_time_pass(entry, PROMPT_GUARD + 1)
    await tick(harness.hub, entry, running=False, end=TurnEnd("error", LIMIT))
    assert at_work(entry).seen == {}
    assert shown(entry) == "idle"
    ended = harness.events("turn_completed")[-1]
    assert ended["stop_reason"] == "error" and ended["limit"]["window_minutes"] == 300


async def test_an_ordinary_end_leaves_the_subagents_the_turn_started_running(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await start(harness, "a1")
    await tick(harness.hub, entry, running=False, end=TurnEnd())
    assert list(at_work(entry).seen) == ["a1"]
    assert shown(entry) == "running"


@pytest.mark.parametrize("ended", [TurnEnd("interrupted"), TurnEnd("error", LIMIT)])
async def test_a_subagent_that_starts_after_the_turn_ended_this_way_is_not_cleared_by_it(
    harness: Harness, ended: TurnEnd
) -> None:
    """The end stays on the transcript until the next turn begins; it counts once."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    state = entry.shared
    assert state is not None and state.prompted_at is not None
    let_time_pass(entry, PROMPT_GUARD + 1)
    await tick(harness.hub, entry, running=False, end=ended)
    await start(harness, "teammate-1")
    await tick(harness.hub, entry, running=False, end=ended)
    assert list(at_work(entry).seen) == ["teammate-1"]
    assert shown(entry) == "running"


async def test_stop_from_an_app_during_the_handoff_ends_the_turn_as_interrupted(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    await end(harness, "a1")
    assert shown(entry) == "running", "the handoff"
    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert shown(entry) == "idle"
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"


async def test_the_same_session_registering_again_keeps_its_subagents(
    harness: Harness,
) -> None:
    """A bridge that reconnects before the old connection is gone: the CLI is the same."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await start(harness, "a1")
    await hook(harness, "stop", background=2, listed=3)
    await tick(harness.hub, entry, running=False)
    assert shown(entry) == "running"

    await harness.hub.attach_registered(FakeAttachment())
    state = entry.shared
    assert state is not None
    assert list(state.agents.seen) == ["a1"]
    assert (state.background, state.background_at > 0) == (2, True)
    assert shown(entry) == "running"
    assert harness.events("turn_completed") == []


async def test_the_same_cli_registering_again_keeps_its_turn_under_way(
    harness: Harness,
) -> None:
    """No "Turn finished" and no turn begun again at the next read, ten seconds later."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    reader = Reader()
    harness.hub.shared.reader = reader

    await harness.hub.attach_registered(FakeAttachment())
    state = entry.shared
    assert state is not None and state.running and state.hooked
    assert state.prompted_at is not None
    assert shown(entry) == "running"
    assert harness.events("turn_completed") == []
    assert len(harness.events("turn_started")) == 1
    assert reader.looks == [("sess-1", 0.0)], "and the transcript is asked at once, not at the scan"


async def test_a_prompt_the_cli_still_waits_on_stays_answerable_when_it_registers_again(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await start(harness, "a1")
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r1", "tool_name": "Bash", "input_preview": '{"command": "make all"}'},
    )
    answering = FakeHookQuestion()
    await harness.hub.attach_question(answering)
    request_id = harness.events("approval")[-1]["request_id"]

    again = FakeAttachment()
    await harness.hub.attach_registered(again)
    state = entry.shared
    assert state is not None and state.attachment is again
    assert list(state.approvals) == [request_id] and state.by_channel == {"r1": request_id}
    assert state.question is not None and state.question.hook is answering
    assert shown(entry) == "needs_approval"
    assert [event["status"] for event in harness.events("approval")] == ["pending"]

    await harness.hub.approve(
        {"session_id": "sess-1", "request_id": request_id, "option_id": "allow"}
    )
    assert again.verdicts == [("r1", "allow")], "the verdict goes out through the new connection"
    assert harness.events("approval")[-1]["status"] == "resolved"


async def test_another_process_registering_for_the_same_session_starts_over(
    harness: Harness,
) -> None:
    """Its subagents, tasks and turn are the old CLI's, which this is not."""
    entry = await harness.attach()
    await hook(harness, "prompt", "person")
    await tick(harness.hub, entry, running=True)
    await start(harness, "a1")
    await hook(harness, "stop", background=2, listed=3)
    other = FakeAttachment()
    other.pid = 5151

    await harness.hub.attach_registered(other)
    state = entry.shared
    assert state is not None and state.attachment is other
    assert not state.running and not state.hooked
    assert state.agents.seen == {} and state.background == 0
    assert shown(entry) == "idle"

    # The old bridge's close, which comes a moment later, is the old CLI's and changes nothing.
    await harness.hub.shared.closed(entry, harness.attachment, "none")
    assert entry.shared is state
    assert shown(entry) == "idle"


async def test_another_process_taking_over_expires_what_the_old_one_waited_on(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    await harness.hub.attach_permission_request(
        harness.attachment,
        {"request_id": "r1", "tool_name": "Bash", "input_preview": '{"command": "make all"}'},
    )
    answering = FakeHookQuestion()
    await harness.hub.attach_question(answering)
    assert shown(entry) == "needs_approval"

    other = FakeAttachment()
    other.pid = 5151
    await harness.hub.attach_registered(other)
    state = entry.shared
    assert state is not None and state.approvals == {} and state.question is None
    assert harness.events("approval")[-1]["status"] == "expired"
    assert harness.events("question")[-1]["status"] == "expired"
    assert answering.replies == [None], "the hook that waited is told to stand down"
    assert shown(entry) == "idle"


async def test_a_bridge_that_names_no_pid_is_never_taken_for_the_same_cli(
    harness: Harness,
) -> None:
    harness.attachment.pid = 0
    entry = await harness.attach()
    await start(harness, "a1")
    other = FakeAttachment()
    other.pid = 0

    await harness.hub.attach_registered(other)
    state = entry.shared
    assert state is not None and state.attachment is other
    assert state.agents.seen == {}
    assert shown(entry) == "idle"


# ------------------------------------------------------------ the bookkeeping


def test_only_the_newest_subagents_are_remembered() -> None:
    work = AgentsAtWork()
    for number in range(MAX_AGENTS + 5):
        work.start(f"a{number}")
    assert len(work.seen) == MAX_AGENTS
    assert "a0" not in work.seen and f"a{MAX_AGENTS + 4}" in work.seen


def test_a_sign_of_life_moves_the_hold_forward_and_never_back() -> None:
    work = AgentsAtWork()
    work.start("a1")
    began, first = work.seen["a1"].began, work.seen["a1"].alive
    work.touch("a1", time.time() - 3600)
    assert work.seen["a1"].alive == first, "a write an hour ago says nothing new"
    work.touch("a1", time.time() + 5)
    assert work.seen["a1"].alive > first
    assert work.seen["a1"].began == began, "and the start stays where it was"
    work.touch("never-started", time.time())
    assert "never-started" not in work.seen
    work.seen["a1"].alive -= AGENT_SILENCE + 1
    assert not work.running
    work.touch("a1", time.time())
    assert work.running


def test_a_wait_is_given_back_up_to_the_credit_and_no_further() -> None:
    work = AgentsAtWork()
    work.start("a1")
    work.seen["a1"].alive -= 2400.0  # silent since just before a prompt raised forty minutes ago
    work.waited(2400.0)
    assert work.running, "forty minutes of waiting is no silence"

    other = AgentsAtWork()
    other.start("a1")
    other.seen["a1"].alive -= 3 * 3600.0
    other.waited(3 * 3600.0)
    assert WAIT_CREDIT < 3 * 3600.0
    assert not other.running, "three hours is no more to go on than the credit"


def test_starting_again_keeps_one_entry_and_the_newest_time() -> None:
    work = AgentsAtWork()
    work.start("a1")
    work.start("a2")
    first = work.seen["a1"].began
    work.start("a1")
    assert list(work.seen) == ["a2", "a1"]
    assert work.seen["a1"].began >= first


def test_an_escape_aborts_the_subagents_that_began_since_and_spares_the_ones_before() -> None:
    work = AgentsAtWork()
    work.start("early")
    work.start("late")
    work.seen["early"].began -= 10.0
    work.abort(time.monotonic() - 5.0)
    assert list(work.seen) == ["early"]
    assert work.running


def test_an_escape_with_nothing_to_tell_the_subagents_by_aborts_them_all() -> None:
    work = AgentsAtWork()
    work.start("a1")
    work.start("a2")
    work.abort(None)
    assert work.seen == {} and not work.running


def test_a_frame_the_hook_sends_is_the_event_the_daemon_reads() -> None:
    """The two halves of the wire agree, end to end, on every subagent event."""
    for name, event in (("SubagentStart", "agent_start"), ("SubagentStop", "agent_stop")):
        frame = turnhook.frame(
            {"session_id": "sess-1", "hook_event_name": name, "agent_id": "a1", "agent_type": "x"}
        )
        assert frame is not None
        read = attach_module._turn_hook(frame)
        assert read is not None and (read.event, read.agent_id) == (event, "a1")
