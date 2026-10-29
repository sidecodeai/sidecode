"""A send never starts a second Claude on a transcript a live terminal CLI is writing.

A session whose terminal CLI holds it is shown as held by nobody from a restart until the CLI's
bridge dials back, and a send that was parked for the daemon, or came in just after it, used to
find it that way and resume it: a second Claude process in the same conversation. It now waits for
the bridge, which registers within seconds, and goes through it; or, when no bridge comes, it is
refused as something to retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client import procscan
from rc_client.agents.claude import holders as claude_holders
from rc_client.agents.claude import transcripts
from rc_client.agents.claude.holders import Holder, HolderScan
from rc_client.errors import RcError, TerminalNotReady
from rc_client.models import AgentInfo, Choice
from rc_client.sessions import hub as hub_module
from rc_client.sessions.attach import SessionStart
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.resume import (
    MAX_DEFERRALS,
    RESUME_PROMPT,
    TERMINAL_CLOSED,
    ResumeScheduler,
)
from tests.test_hub import FakeRunner
from tests.test_live_records import STARTED, record, write
from tests.test_resume import Clock
from tests.test_session_start import TERMINAL_PID, Harness, no_holder, said

HELD = "held-1"
MINUTE = 60 * 1000


class Terminals:
    """The live CLIs a process scan finds, and how often it was asked."""

    def __init__(self) -> None:
        self.holders: list[Holder] = []
        self.scans = 0
        self.cwds: list[bool] = []
        # Whether the process table could be read at all.
        self.complete = True

    def cli(
        self,
        session_id: str | None = HELD,
        *,
        attachable: bool = True,
        pid: int = TERMINAL_PID,
        recorded_at: int = 0,
    ) -> None:
        self.holders.append(
            Holder(
                pid=pid,
                identity=(pid, "a"),
                session_id=session_id,
                cwd=None,
                attachable=attachable,
                recorded_at=recorded_at,
            )
        )

    async def scan(self, exclude_pids: set[int] | None = None, *, cwds: bool = True) -> HolderScan:
        self.scans += 1
        self.cwds.append(cwds)
        return HolderScan(holders=list(self.holders), complete=self.complete)


@pytest.fixture
def terminals(monkeypatch: pytest.MonkeyPatch) -> Terminals:
    found = Terminals()
    monkeypatch.setattr(claude_holders, "scan_holders", found.scan)
    return found


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)
    monkeypatch.setattr(hub_module, "BRIDGE_WAIT", 3.0)
    built = Harness(tmp_path)
    yield built
    await built.hub.close()
    built.close()


@pytest.fixture
def no_bridge_comes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait a send gives a bridge, cut short for a test that is sure none will register."""
    monkeypatch.setattr(hub_module, "BRIDGE_WAIT", 0.2)


class Resumes:
    """The Claude processes the hub started for a session, which a guarded send never does."""

    def __init__(self, harness: Harness) -> None:
        self.runners: list[FakeRunner] = []
        harness.hub._build_runner = self.build  # type: ignore[method-assign]

    async def build(self, entry: SessionEntry, info: Any, resume: str | None) -> FakeRunner:
        runner = FakeRunner(entry.channel)
        self.runners.append(runner)
        return runner


@pytest.fixture
def resumes(harness: Harness) -> Resumes:
    return Resumes(harness)


def send(harness: Harness, text: str = "go on", **fields: Any) -> Any:
    params = {"id": "req-1", "session_id": HELD, "text": text, **fields}
    return harness.hub.send(params)


def known(harness: Harness, **fields: Any) -> SessionEntry:
    """The session as the registry left it after a restart: nothing holds it yet."""
    return harness.seed(HELD, **fields)


# ------------------------------------------------------ a bridge that is coming


async def test_a_send_waits_for_the_cli_to_attach_and_goes_through_its_bridge(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    known(harness)
    terminals.cli()

    sending = asyncio.create_task(send(harness))
    await asyncio.sleep(0.1)
    assert not sending.done(), "the send waits for the bridge, it neither fails nor resumes"

    attachment = await harness.attach(HELD)
    assert await asyncio.wait_for(sending, timeout=2) == {"accepted": "sent"}
    assert [text for _, text in attachment.injected] == ["go on"]
    assert resumes.runners == []
    assert harness.hub.entry(HELD).runner is None


async def test_a_session_the_scan_already_shows_as_the_terminals_is_refused_as_it_always_was(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """Its CLI is found, so it has the answer every terminal session has; nothing is looked up."""
    entry = known(harness)
    entry.session.control = "terminal"
    terminals.cli()

    started = asyncio.get_running_loop().time()
    with pytest.raises(RcError) as refused:
        await send(harness)

    assert refused.value.code == "conflict"
    assert refused.value.message == "controlled by terminal; take over first"
    assert asyncio.get_running_loop().time() - started < hub_module.BRIDGE_WAIT / 2
    assert terminals.scans == 0
    assert resumes.runners == []


async def test_a_command_to_a_session_the_scan_shows_as_the_terminals_is_refused_as_it_always_was(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    harness.hub._agents = lambda: [  # the stock agent takes no commands from here
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["commands"],
        )
    ]
    entry = known(harness)
    entry.session.control = "terminal"
    terminals.cli()

    with pytest.raises(RcError) as refused:
        await harness.hub.command({"id": "c-1", "session_id": HELD, "name": "compact"})

    assert refused.value.code == "conflict"
    assert terminals.scans == 0
    assert resumes.runners == []


async def test_a_command_waits_for_the_bridge_and_is_refused_when_none_comes(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    harness.hub._agents = lambda: [  # the stock agent takes no commands from here
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["commands"],
        )
    ]
    known(harness)
    terminals.cli()
    with pytest.raises(RcError) as refused:
        await harness.hub.command({"id": "c-1", "session_id": HELD, "name": "compact"})
    assert refused.value.code == "timeout"
    assert resumes.runners == []


# ------------------------------------------------------------ one that is not


async def test_a_send_is_refused_as_retryable_when_no_bridge_comes(
    harness: Harness,
    terminals: Terminals,
    resumes: Resumes,
    no_bridge_comes: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    known(harness)
    terminals.cli()

    with pytest.raises(RcError) as refused:
        await send(harness)

    assert refused.value.code == "timeout"
    assert refused.value.message == hub_module.BRIDGE_PENDING
    said = [record.getMessage() for record in caplog.records]
    assert "the bridge of a terminal that holds a session did not register in time" in said
    assert resumes.runners == []
    assert harness.hub.entry(HELD).runner is None

    # The refusal is not remembered, so the app's retry, under the same id, goes through.
    attachment = await harness.attach(HELD)
    assert await send(harness) == {"accepted": "sent"}
    assert [text for _, text in attachment.injected] == ["go on"]


async def test_a_cli_the_shim_did_not_start_is_the_terminals_and_is_refused_at_once(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    known(harness)
    terminals.cli(attachable=False)

    started = asyncio.get_running_loop().time()
    with pytest.raises(RcError) as refused:
        await send(harness)

    assert refused.value.code == "conflict"
    assert refused.value.message == "controlled by terminal; take over first"
    waited = asyncio.get_running_loop().time() - started
    assert waited < hub_module.BRIDGE_WAIT / 2, "no bridge will come to wait for"
    assert resumes.runners == []


# ------------------------------------------------------- how a holder is found


async def test_the_hooks_word_finds_a_cli_that_names_no_session(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    known(harness)
    terminals.cli(session_id=None)
    harness.hub.bindings.remember(SessionStart(HELD, "/repo", TERMINAL_PID, "resume", ""), "a")

    with pytest.raises(RcError) as refused:
        await send(harness)

    assert refused.value.code == "timeout"
    assert resumes.runners == []


async def test_a_cli_that_a_newer_record_puts_in_another_session_does_not_hold_this_one(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """The hook said this session once; Claude Code's record says the CLI has moved on since."""
    known(harness)
    harness.hub.bindings.remember(SessionStart(HELD, "/repo", TERMINAL_PID, "resume", ""), "a")
    terminals.cli(session_id="elsewhere-1", recorded_at=2**62)

    assert await send(harness) == {"accepted": "sent"}
    assert len(resumes.runners) == 1


async def test_a_hook_heard_after_the_record_still_puts_the_cli_in_this_session(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    known(harness)
    harness.hub.bindings.remember(SessionStart(HELD, "/repo", TERMINAL_PID, "resume", ""), "a")
    terminals.cli(session_id="elsewhere-1", recorded_at=1)

    with pytest.raises(RcError) as refused:
        await send(harness)

    assert refused.value.code == "timeout"
    assert resumes.runners == []


async def test_a_pid_that_a_later_process_was_given_is_not_the_cli_the_hook_spoke_of(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    known(harness)
    terminals.cli(session_id=None)
    harness.hub.bindings.remember(SessionStart(HELD, "/repo", TERMINAL_PID, "resume", ""), "other")

    assert await send(harness) == {"accepted": "sent"}
    assert len(resumes.runners) == 1, "nothing holds the session, so it is resumed as ever"


async def test_a_cli_in_another_session_does_not_hold_this_one(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    known(harness)
    terminals.cli(session_id="elsewhere-1")

    assert await send(harness) == {"accepted": "sent"}
    assert len(resumes.runners) == 1


async def test_claude_codes_record_finds_the_cli_through_the_whole_scan(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, resumes: Resumes, no_bridge_comes: None
) -> None:
    """From the file on disk, through the process table, to the refusal."""
    known(harness)
    write(TERMINAL_PID, record(pid=TERMINAL_PID, sessionId=HELD, procStart=STARTED))

    async def table() -> procscan.Scan:
        command = "/usr/local/bin/claude --dangerously-load-development-channels server:rc"
        return procscan.Scan(
            procs=[procscan.Proc(pid=TERMINAL_PID, ppid=1, start=STARTED, command=command)],
            complete=True,
        )

    async def cwds(pids: list[int]) -> tuple[dict[int, str], bool]:
        raise AssertionError("a send has no use for working directories")

    monkeypatch.setattr(claude_holders, "scan_processes", table)
    monkeypatch.setattr(claude_holders, "process_cwds", cwds)

    with pytest.raises(RcError) as refused:
        await send(harness)

    assert refused.value.code == "timeout"
    assert resumes.runners == []


# -------------------------------------------------------- what is not guarded


async def test_a_session_nothing_holds_is_resumed_as_it_always_was(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    known(harness)

    assert await send(harness) == {"accepted": "sent"}

    [runner] = resumes.runners
    assert runner.sent == ["go on"]
    assert terminals.cwds == [False]


async def test_a_session_of_another_agent_is_never_looked_up(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    harness.seed(HELD, agent="codex")
    terminals.cli()
    with pytest.raises(RcError) as refused:
        await send(harness)
    # This device has no Codex, which is what refuses it; no process was scanned for it.
    assert refused.value.code == "agent_unavailable"
    assert terminals.scans == 0


async def test_a_session_this_device_drives_is_never_looked_up(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    entry = known(harness)
    runner = FakeRunner(entry.channel)
    entry.runner = runner
    terminals.cli()

    assert await send(harness) == {"accepted": "sent"}
    assert terminals.scans == 0
    assert runner.sent == ["go on"]


async def test_a_session_already_attached_is_never_looked_up(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    await harness.attach(HELD)
    terminals.cli()

    assert await send(harness) == {"accepted": "sent"}
    assert terminals.scans == 0


async def test_a_session_whose_terminal_cli_has_exited_is_resumed_as_it_always_was(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, resumes: Resumes
) -> None:
    """The CLI was attached, had spoken a hook and had a record; then it exited. Nothing holds the
    session, so the next send resumes it, with what Claude Code left behind in the way."""

    async def started(pid: int) -> str | None:
        return STARTED

    harness.hub.process_start = started
    write(TERMINAL_PID, record(pid=TERMINAL_PID, sessionId=HELD, procStart=STARTED))
    attachment = await harness.attach(HELD)
    await harness.hook(HELD, source="resume")
    entry = harness.hub.entry(HELD)
    await said(entry, "run the tests")
    assert harness.hub.bindings.get(TERMINAL_PID) is not None

    # It exits: its bridge closes, and the process table no longer has it.
    no_holder(harness, monkeypatch)
    await harness.hub.attach_closed(attachment)
    assert entry.session.control == "none"
    assert harness.hub.bindings.get(TERMINAL_PID) is None

    async def table() -> procscan.Scan:
        return procscan.Scan(procs=[], complete=True)

    monkeypatch.setattr(claude_holders, "scan_processes", table)

    assert await send(harness) == {"accepted": "sent"}

    [runner] = resumes.runners
    assert runner.sent == ["go on"]
    assert entry.runner is runner and str(entry.session.control) == "remote"


async def test_taking_over_a_terminal_session_still_resumes_it_without_a_look_for_a_holder(
    harness: Harness, terminals: Terminals, resumes: Resumes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Take over ends the CLI itself and resumes the session, and the guard is not part of it."""
    entry = known(harness)
    entry.session.control = "terminal"
    entry.holder_pid, entry.holder_identity = TERMINAL_PID, (TERMINAL_PID, "a")
    terminals.cli()

    async def released(pid: int, identity: tuple[int, str]) -> bool:
        return (pid, identity) == (TERMINAL_PID, (TERMINAL_PID, "a"))

    monkeypatch.setattr(claude_holders, "release_holder", released)

    reply = await harness.hub.takeover({"session_id": HELD})

    assert reply["session"]["control"] == "remote"
    assert len(resumes.runners) == 1
    assert terminals.scans == 0


# --------------------------------------- a table that cannot be read is not an empty one


async def test_a_process_table_that_cannot_be_read_is_not_a_reason_to_resume(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """Not knowing who holds a session is not the same as nobody holding it."""
    known(harness)
    terminals.complete = False

    with pytest.raises(TerminalNotReady) as refused:
        await send(harness)

    assert refused.value.code == "timeout"
    assert refused.value.message == hub_module.SCAN_FAILED
    assert resumes.runners == []

    # Nothing was remembered, so the retry, once the table can be read, resumes it as ever.
    terminals.complete = True
    assert await send(harness) == {"accepted": "sent"}
    assert len(resumes.runners) == 1


async def test_a_ps_that_fails_leaves_the_session_alone_all_the_way_down(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, resumes: Resumes
) -> None:
    """From the process scan itself, which says `complete=False` when `ps` fails or times out."""
    known(harness)

    async def table() -> procscan.Scan:
        return procscan.Scan(procs=[], complete=False)

    monkeypatch.setattr(claude_holders, "scan_processes", table)

    with pytest.raises(TerminalNotReady):
        await send(harness)

    assert resumes.runners == []


# --------------------------- a repeat of a request in flight is not served a second time


async def test_a_retry_of_a_send_that_is_still_waiting_for_the_bridge_shares_its_outcome(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """An app retries under the same id when its request times out, and the first is still going."""
    known(harness)
    terminals.cli()
    first = asyncio.create_task(send(harness, "same words", id="r-1"))
    await asyncio.sleep(0.2)
    retry = asyncio.create_task(send(harness, "same words", id="r-1"))
    await asyncio.sleep(0.2)

    attachment = await harness.attach(HELD)

    assert await asyncio.wait_for(first, timeout=5) == {"accepted": "sent"}
    assert await asyncio.wait_for(retry, timeout=5) == {"accepted": "sent"}
    assert [text for _, text in attachment.injected] == ["same words"]
    assert harness.hub.entry(HELD).queue == []
    assert terminals.scans == 1, "the repeat looked nothing up"
    assert resumes.runners == []


async def test_a_retry_shares_the_refusal_of_the_send_it_repeats(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    known(harness)
    terminals.cli()
    first = asyncio.create_task(send(harness, id="r-1"))
    await asyncio.sleep(0.05)
    retry = asyncio.create_task(send(harness, id="r-1"))

    outcomes = await asyncio.gather(first, retry, return_exceptions=True)

    assert [type(outcome) for outcome in outcomes] == [TerminalNotReady, TerminalNotReady]
    assert terminals.scans == 1
    assert harness.hub._flying == {}, "nothing is left waiting for an answer"


async def test_a_send_that_finished_is_answered_from_its_record_and_not_sent_again(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    attachment = await harness.attach(HELD)
    assert await send(harness, "once", id="r-1") == {"accepted": "sent"}

    assert await send(harness, "once", id="r-1") == {"accepted": "sent"}

    assert [text for _, text in attachment.injected] == ["once"]


async def test_sends_with_other_ids_are_each_served(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    known(harness)
    terminals.cli()
    first = asyncio.create_task(send(harness, "one", id="r-1"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(send(harness, "two", id="r-2"))
    await asyncio.sleep(0.05)
    attachment = await harness.attach(HELD)

    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert [text for _, text in attachment.injected] == ["one"]
    assert [item["id"] for item in harness.hub.entry(HELD).queue] == ["r-2"]


async def test_a_command_repeated_while_it_waits_shares_the_first_ones_outcome(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    harness.hub._agents = lambda: [  # the stock agent takes no commands from here
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["commands"],
        )
    ]
    known(harness)
    terminals.cli()
    params = {"id": "c-1", "session_id": HELD, "name": "compact"}
    first = asyncio.create_task(harness.hub.command(dict(params)))
    await asyncio.sleep(0.05)
    retry = asyncio.create_task(harness.hub.command(dict(params)))

    outcomes = await asyncio.gather(first, retry, return_exceptions=True)

    assert [type(outcome) for outcome in outcomes] == [TerminalNotReady, TerminalNotReady]
    assert terminals.scans == 1


# ---------------------------------- messages reach the terminal in the order they were sent


async def test_two_sends_that_wait_for_the_bridge_reach_the_terminal_in_the_order_sent(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """The second used to poll on a clock of its own and could get in ahead of the first."""
    known(harness)
    terminals.cli()
    first = asyncio.create_task(send(harness, "first", id="r-1"))
    await asyncio.sleep(0.025)
    second = asyncio.create_task(send(harness, "second", id="r-2"))
    await asyncio.sleep(0.035)

    attachment = await harness.attach(HELD)
    replies = list(await asyncio.wait_for(asyncio.gather(first, second), timeout=5))

    assert replies == [{"accepted": "sent"}, {"accepted": "queued", "queued_id": "r-2"}]
    assert [text for _, text in attachment.injected] == ["first"]
    assert [item["text"] for item in harness.hub.entry(HELD).queue] == ["second"]


async def test_sends_to_a_session_are_served_one_at_a_time_in_arrival_order(
    harness: Harness, terminals: Terminals, resumes: Resumes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even where nothing waits for a bridge, two scans can finish in either order."""
    entry = known(harness)
    looked: list[str] = []

    async def first_is_slow(self: Any, entry: SessionEntry, arrived: float) -> None:
        looked.append("looked")
        await asyncio.sleep(0.05 if len(looked) == 1 else 0.0)

    monkeypatch.setattr(hub_module.SessionHub, "_await_terminal", first_is_slow)
    tasks = [
        asyncio.create_task(
            harness.hub.send({"id": f"id-{text}", "session_id": HELD, "text": text})
        )
        for text in ("a", "b", "c")
    ]

    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

    [runner] = resumes.runners
    assert runner.sent == ["a"], "the first to arrive is the one that starts the turn"
    assert [item["text"] for item in entry.queue] == ["b", "c"]


# ---------------------------------- a resume after a usage limit that the guard turned away


def resume_rows(harness: Harness) -> list[str]:
    return [
        str(frame["event"]["status"])
        for frame in harness.frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "resume"
    ]


async def test_a_resume_the_guard_turns_away_is_tried_again_and_not_dropped(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    """Nobody will ask for it again, and the wait is a restart's: it goes through a minute later."""
    clock = Clock()
    scheduler = ResumeScheduler(harness.hub, clock)
    harness.hub.resumes = scheduler
    entry = known(harness)
    terminals.cli()
    await scheduler.set_request({"session_id": HELD, "at": clock.now + 2 * MINUTE})
    clock.advance(3 * MINUTE)

    await scheduler.tick()

    assert resumes.runners == []
    assert resume_rows(harness) == ["scheduled", "fired", "rescheduled"]
    assert entry.session.resume is not None and entry.session.resume.at == clock.now + MINUTE
    assert scheduler._deferred == {HELD: 1}

    # Its CLI dials back, and a minute later the resume goes through the bridge.
    attachment = await harness.attach(HELD)
    clock.advance(MINUTE + 1)
    await scheduler.tick()

    assert [text for _, text in attachment.injected] == [RESUME_PROMPT]
    assert resumes.runners == []
    assert scheduler._deferred == {}
    assert resume_rows(harness)[-1] == "fired"


async def test_a_resume_the_guard_keeps_turning_away_is_given_up_after_a_few_tries(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    clock = Clock()
    scheduler = ResumeScheduler(harness.hub, clock)
    harness.hub.resumes = scheduler
    entry = known(harness)
    terminals.cli()
    await scheduler.set_request({"session_id": HELD, "at": clock.now + 2 * MINUTE})
    clock.advance(3 * MINUTE)

    for _ in range(MAX_DEFERRALS + 1):
        await scheduler.tick()
        clock.advance(MINUTE + 1)

    assert resume_rows(harness) == [
        "scheduled",
        *["fired", "rescheduled"] * MAX_DEFERRALS,
        "fired",
        "dropped",
    ]
    assert entry.session.resume is None
    assert scheduler._deferred == {} and scheduler._records == {}
    assert resumes.runners == []


async def test_a_resume_that_fails_for_a_reason_no_wait_mends_is_dropped_at_once(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """Only a refusal a restart's wait ends is put off; a Claude that cannot start is not one."""
    clock = Clock()
    scheduler = ResumeScheduler(harness.hub, clock)
    harness.hub.resumes = scheduler
    known(harness)

    async def broken(entry: SessionEntry, info: Any, resume: str | None) -> Any:
        raise RcError("agent_unavailable", "claude is not installed")

    harness.hub._build_runner = broken  # type: ignore[method-assign]
    await scheduler.set_request({"session_id": HELD, "at": clock.now + 2 * MINUTE})
    clock.advance(3 * MINUTE)

    await scheduler.tick()

    assert resume_rows(harness) == ["scheduled", "fired", "dropped"]
    assert scheduler._records == {}


# ------------------------- a person's message cancels a resume as it arrives, not at the gate


async def test_a_message_waiting_at_the_gate_still_cancels_a_resume_that_falls_due(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """Behind a long request, the person's words must not be followed by the resume prompt."""
    clock = Clock()
    scheduler = ResumeScheduler(harness.hub, clock)
    harness.hub.resumes = scheduler
    entry = known(harness)
    await scheduler.set_request({"session_id": HELD, "at": clock.now + 2 * MINUTE})

    async with entry.gate:
        person = asyncio.create_task(send(harness, "person says", id="p-1"))
        await asyncio.sleep(0.05)
        assert not person.done(), "it waits at the gate behind the request that holds it"
        assert resume_rows(harness) == ["scheduled", "cancelled"], "and has cancelled the resume"
        clock.advance(3 * MINUTE)
        await scheduler.tick()
        assert resumes.runners == [], "so nothing is due when the time passes"

    assert await asyncio.wait_for(person, timeout=5) == {"accepted": "sent"}
    [runner] = resumes.runners
    assert runner.sent == ["person says"]
    assert entry.queue == []
    assert resume_rows(harness) == ["scheduled", "cancelled"]


async def test_a_command_waiting_at_the_gate_cancels_a_resume_too(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    harness.hub._agents = lambda: [  # the stock agent takes no commands from here
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["commands"],
        )
    ]
    clock = Clock()
    scheduler = ResumeScheduler(harness.hub, clock)
    harness.hub.resumes = scheduler
    entry = known(harness)
    await scheduler.set_request({"session_id": HELD, "at": clock.now + 2 * MINUTE})

    async with entry.gate:
        waiting = asyncio.create_task(
            harness.hub.command({"id": "c-1", "session_id": HELD, "name": "compact"})
        )
        await asyncio.sleep(0.05)
        assert resume_rows(harness) == ["scheduled", "cancelled"]
        waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)


# ------------------------------ sends that queue behind one another wait for a bridge together


async def test_sends_queued_for_a_bridge_that_never_comes_are_refused_together(
    harness: Harness, terminals: Terminals, resumes: Resumes, no_bridge_comes: None
) -> None:
    """Each used to wait out its own turn at the gate: the fourth was refused after four waits."""
    known(harness)
    terminals.cli()
    loop = asyncio.get_running_loop()
    started = loop.time()
    sends = [asyncio.create_task(send(harness, f"m-{n}", id=f"r-{n}")) for n in range(4)]

    outcomes = await asyncio.wait_for(asyncio.gather(*sends, return_exceptions=True), timeout=5)

    waited = loop.time() - started
    assert [type(outcome) for outcome in outcomes] == [TerminalNotReady] * 4
    assert waited < hub_module.BRIDGE_WAIT * 2, "one wait, not one wait for each"
    assert resumes.runners == []


async def test_a_send_that_queued_while_the_bridge_came_is_not_refused(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """Counting from arrival must not refuse what arrived early and was held up, only waited."""
    known(harness)
    terminals.cli()
    first = asyncio.create_task(send(harness, "first", id="r-1"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(send(harness, "second", id="r-2"))
    await asyncio.sleep(0.05)
    attachment = await harness.attach(HELD)

    replies = list(await asyncio.wait_for(asyncio.gather(first, second), timeout=5))

    assert replies == [{"accepted": "sent"}, {"accepted": "queued", "queued_id": "r-2"}]
    assert [text for _, text in attachment.injected] == ["first"]


# ------------- a resume that falls due in the window after a restart is put off, whoever refuses


async def reattached_after_a_restart(
    harness: Harness, control: str
) -> tuple[ResumeScheduler, Clock, SessionEntry]:
    """A shared session with a resume pending, then the restart: nothing attached any more."""
    clock = Clock()
    scheduler = ResumeScheduler(harness.hub, clock)
    harness.hub.resumes = scheduler
    attachment = await harness.attach(HELD)
    entry = harness.hub.entry(HELD)
    await said(entry, "run the tests")
    await scheduler.set_request({"session_id": HELD, "at": clock.now + 2 * MINUTE})
    assert scheduler._records[HELD].control == "shared"
    await harness.hub.shared.closed(entry, attachment, control)
    clock.advance(3 * MINUTE)
    return scheduler, clock, entry


async def test_a_resume_due_while_the_session_reads_as_held_by_nobody_is_put_off(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    scheduler, clock, entry = await reattached_after_a_restart(harness, "none")

    await scheduler.tick()

    assert resume_rows(harness) == ["scheduled", "rescheduled"]
    assert entry.session.resume is not None and scheduler._deferred == {HELD: 1}
    assert resumes.runners == []

    # Its CLI's bridge dials back, and a minute later the resume goes through it.
    attachment = await harness.attach(HELD)
    clock.advance(MINUTE + 1)
    await scheduler.tick()

    assert [text for _, text in attachment.injected] == [RESUME_PROMPT]
    assert resume_rows(harness)[-1] == "fired"
    assert scheduler._deferred == {} and resumes.runners == []


async def test_a_resume_due_while_the_scan_shows_the_session_as_the_terminals_is_put_off(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    """Refused with a conflict, as a send to a terminal session is, until its bridge is back."""
    scheduler, clock, entry = await reattached_after_a_restart(harness, "terminal")

    await scheduler.tick()

    assert resume_rows(harness) == ["scheduled", "fired", "rescheduled"]
    assert entry.session.resume is not None and scheduler._deferred == {HELD: 1}

    attachment = await harness.attach(HELD)
    clock.advance(MINUTE + 1)
    await scheduler.tick()

    assert [text for _, text in attachment.injected] == [RESUME_PROMPT]
    assert scheduler._deferred == {}


async def test_a_resume_that_keeps_finding_nobody_is_dropped_with_the_reason_it_met(
    harness: Harness, terminals: Terminals, resumes: Resumes
) -> None:
    scheduler, clock, entry = await reattached_after_a_restart(harness, "none")

    for _ in range(MAX_DEFERRALS + 1):
        await scheduler.tick()
        clock.advance(MINUTE + 1)

    assert resume_rows(harness) == ["scheduled", *["rescheduled"] * MAX_DEFERRALS, "dropped"]
    dropped = [
        frame["event"]
        for frame in harness.frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "resume"
    ][-1]
    assert dropped["reason"] == TERMINAL_CLOSED
    assert entry.session.resume is None and scheduler._records == {}
