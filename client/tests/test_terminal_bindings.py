"""A restart does not send an attached CLI back to the session it started in.

The channel bridge reports the session id the CLI had when it was spawned, whatever `/resume`,
`/clear` or a compaction made of it since. The `SessionStart` hook is the only account of those, and
it used to live in the daemon's memory alone, so every restart attached each moved CLI to a session
nobody was in until its next hook. The hook's word is stored now, with the CLI's start time and
the time it was heard, and Claude Code's own record of the CLI is set against it: where they
disagree, the later word names the session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rc_client.agents.claude import transcripts
from rc_client.agents.claude.holders import Holder, HolderScan
from rc_client.agents.codex import rollouts
from rc_client.sessions import bindings as bindings_module
from rc_client.sessions import hub as hub_module
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.attach import SessionStart
from rc_client.sessions.bindings import KEY, MAX_BINDINGS, TerminalBindings
from rc_client.sessions.mirror import MirrorService
from tests.test_live_records import record, write
from tests.test_session_start import TERMINAL_PID, Harness, no_holder, said

STARTED = "Tue Oct 27 14:39:20 2026"
LATER = "Wed Oct 28 01:02:03 2026"
MOVED = "6f1d2c3a-0b4e-4f5a-9c7d-1a2b3c4d5e6f"
# When a hook is heard in these tests, in milliseconds, and how far apart the tests set the events
# they weigh against it.
NOW = 1_800_000_000_000
MINUTE = 60_000


class Processes:
    """The start time each pid has, as the hub asks for it."""

    def __init__(self) -> None:
        self.starts: dict[int, str | None] = {TERMINAL_PID: STARTED}

    async def __call__(self, pid: int) -> str | None:
        return self.starts.get(pid)


def run_of(tmp_path: Path, processes: Processes) -> Harness:
    """A daemon run: a hub on the registry the previous run left, as `Daemon.run` builds it."""
    harness = Harness(tmp_path)
    harness.hub.process_start = processes
    harness.hub.load()
    return harness


@pytest.fixture(autouse=True)
def quiet_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)


@pytest.fixture(autouse=True)
def hooks_are_heard_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hook is heard at `NOW`, so a record can be set before or after it."""
    monkeypatch.setattr(bindings_module, "now_ms", lambda: NOW)


@pytest.fixture
async def processes() -> AsyncIterator[Processes]:
    yield Processes()


async def hook(harness: Harness, session_id: str = MOVED, transcript_path: str = "") -> None:
    await harness.hook(session_id, source="resume", transcript_path=transcript_path)


# -------------------------------------------------------------- the map itself


def test_what_a_hook_said_is_read_back_by_the_next_run(tmp_path: Path) -> None:
    first = Harness(tmp_path)
    bindings = TerminalBindings(first.registry)
    bindings.remember(
        SessionStart(
            session_id="s-1", cwd="/repo", pid=7, source="resume", transcript_path="/t/s-1.jsonl"
        ),
        STARTED,
    )
    first.close()

    second = Harness(tmp_path)
    reread = TerminalBindings(second.registry)
    reread.load()
    binding = reread.get(7)
    assert binding is not None
    assert (binding.process, binding.at) == (STARTED, NOW)
    assert binding.start == SessionStart("s-1", "/repo", 7, "resume", "/t/s-1.jsonl")
    assert reread.get(8) is None
    second.close()


def test_a_hook_heard_while_the_start_time_was_unreadable_is_this_runs_alone(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    bindings = TerminalBindings(harness.registry)
    bindings.remember(SessionStart("s-1", "/repo", 7, "resume", ""), None)
    assert bindings.get(7) is not None
    assert json.loads(harness.registry.get_kv(KEY) or "[]") == []
    harness.close()


def test_forgetting_a_cli_is_stored_too(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    bindings = TerminalBindings(harness.registry)
    bindings.remember(SessionStart("s-1", "/repo", 7, "resume", ""), STARTED)
    bindings.forget(7)
    assert bindings.get(7) is None
    assert json.loads(harness.registry.get_kv(KEY) or "[]") == []
    harness.close()


def test_the_map_keeps_the_newest_cli_when_it_is_full(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    bindings = TerminalBindings(harness.registry)
    for pid in range(1, MAX_BINDINGS + 6):
        bindings.remember(SessionStart(f"s-{pid}", "/repo", pid, "startup", ""), STARTED)
    assert bindings.get(1) is None
    assert bindings.get(MAX_BINDINGS + 5) is not None
    assert len(json.loads(harness.registry.get_kv(KEY) or "[]")) == MAX_BINDINGS
    harness.close()


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        '{"pid": 7}',
        "[1, 2]",
        '[{"pid": 7, "process": "p"}]',
        '[{"pid": "7", "process": "p", "session_id": "s"}]',
        '[{"pid": true, "process": "p", "session_id": "s"}]',
        '[{"pid": 7, "process": "", "session_id": "s"}]',
        '[{"pid": 0, "process": "p", "session_id": "s"}]',
    ],
)
def test_a_stored_map_that_does_not_decode_is_a_map_with_nothing_in_it(
    tmp_path: Path, stored: str
) -> None:
    harness = Harness(tmp_path)
    harness.registry.set_kv(KEY, stored)
    bindings = TerminalBindings(harness.registry)
    bindings.load()
    assert bindings.get(7) is None
    harness.close()


def test_a_stored_entry_without_a_time_is_older_than_any_record(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.registry.set_kv(KEY, '[{"pid": 7, "process": "p", "session_id": "s"}]')
    bindings = TerminalBindings(harness.registry)
    bindings.load()
    binding = bindings.get(7)
    assert binding is not None and binding.at == 0
    harness.close()


@pytest.mark.parametrize("at", ['"later"', "true", "null", "1.5"])
def test_a_time_that_is_not_a_whole_number_is_no_time(tmp_path: Path, at: str) -> None:
    harness = Harness(tmp_path)
    harness.registry.set_kv(KEY, f'[{{"pid": 7, "process": "p", "session_id": "s", "at": {at}}}]')
    bindings = TerminalBindings(harness.registry)
    bindings.load()
    binding = bindings.get(7)
    assert binding is not None and binding.at == 0
    harness.close()


# -------------------------------------------------------- across a restart


async def test_a_restart_keeps_a_moved_cli_in_the_session_its_hook_put_it_in(
    tmp_path: Path, processes: Processes, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = run_of(tmp_path, processes)
    await before.attach("started-in")
    await hook(before, transcript_path="/t/moved.jsonl")
    before.close()

    after = run_of(tmp_path, processes)
    attachment = await after.attach("started-in")

    assert attachment.session_id == MOVED
    said = [record.getMessage() for record in caplog.records]
    assert "a bridge was put in the session its CLI is in" in said
    assert "started-in" not in after.hub.entries
    entry = after.hub.entry(MOVED)
    assert entry.session.control == "shared"
    assert entry.transcript == "/t/moved.jsonl"
    assert after.removed() == []
    after.close()


async def test_a_session_that_was_already_known_is_the_one_the_cli_is_put_back_in(
    tmp_path: Path, processes: Processes
) -> None:
    """The row the apps know, with its title and history, not a blank one beside it."""
    before = run_of(tmp_path, processes)
    known = before.seed(MOVED, title="Ledger migration")
    known.channel.start()
    await hook(before)
    await before.attach("started-in")
    before.close()

    after = run_of(tmp_path, processes)
    await after.attach("started-in")

    entry = after.hub.entry(MOVED)
    assert entry.session.title == "Ledger migration"
    assert entry.session.control == "shared"
    assert list(after.hub.entries) == [MOVED]
    after.close()


async def test_a_pid_that_a_later_process_was_given_is_not_the_cli_the_hook_spoke_of(
    tmp_path: Path, processes: Processes
) -> None:
    before = run_of(tmp_path, processes)
    await hook(before)
    before.close()

    processes.starts[TERMINAL_PID] = LATER
    after = run_of(tmp_path, processes)
    attachment = await after.attach("started-in")

    assert attachment.session_id == "started-in"
    assert after.hub.entry("started-in").session.control == "shared"
    assert after.hub.bindings.get(TERMINAL_PID) is None, "and the stale word is let go"
    after.close()


async def test_a_start_time_that_cannot_be_read_does_not_discard_the_hooks_word(
    tmp_path: Path, processes: Processes
) -> None:
    before = run_of(tmp_path, processes)
    await hook(before)
    before.close()

    processes.starts[TERMINAL_PID] = None
    after = run_of(tmp_path, processes)
    attachment = await after.attach("started-in")

    assert attachment.session_id == MOVED
    after.close()


async def test_a_hook_heard_without_a_start_time_does_not_outlive_the_run(
    tmp_path: Path, processes: Processes
) -> None:
    processes.starts[TERMINAL_PID] = None
    before = run_of(tmp_path, processes)
    await hook(before)
    attachment = await before.attach("started-in")
    assert attachment.session_id == MOVED, "this run believes it"
    before.close()

    processes.starts[TERMINAL_PID] = STARTED
    after = run_of(tmp_path, processes)
    assert (await after.attach("started-in")).session_id == "started-in"
    after.close()


async def test_a_later_hook_replaces_the_earlier_one(tmp_path: Path, processes: Processes) -> None:
    before = run_of(tmp_path, processes)
    await hook(before, "first-1")
    await hook(before, "second-1")
    before.close()

    after = run_of(tmp_path, processes)
    assert (await after.attach("started-in")).session_id == "second-1"
    after.close()


# ----------------------------------------------- Claude Code's own record


async def test_a_cli_the_hook_never_spoke_of_is_placed_by_claude_codes_record(
    tmp_path: Path, processes: Processes
) -> None:
    """The first restart into this build: the old daemon stored nothing, and the CLI had moved."""
    write(TERMINAL_PID, record(pid=TERMINAL_PID, sessionId=MOVED, procStart=STARTED))
    harness = run_of(tmp_path, processes)

    attachment = await harness.attach("started-in")

    assert attachment.session_id == MOVED
    assert harness.hub.entry(MOVED).session.control == "shared"
    assert "started-in" not in harness.hub.entries
    harness.close()


async def test_a_record_of_another_process_places_nothing(
    tmp_path: Path, processes: Processes
) -> None:
    write(TERMINAL_PID, record(pid=TERMINAL_PID, sessionId=MOVED, procStart=LATER))
    harness = run_of(tmp_path, processes)
    assert (await harness.attach("started-in")).session_id == "started-in"
    harness.close()


async def test_a_record_places_nothing_when_the_process_start_cannot_be_read(
    tmp_path: Path, processes: Processes
) -> None:
    write(TERMINAL_PID, record(pid=TERMINAL_PID, sessionId=MOVED, procStart=STARTED))
    processes.starts[TERMINAL_PID] = None
    harness = run_of(tmp_path, processes)
    assert (await harness.attach("started-in")).session_id == "started-in"
    harness.close()


async def test_a_record_written_after_the_hook_spoke_names_the_session(
    tmp_path: Path, processes: Processes
) -> None:
    """The CLI moved again while the daemon was down, and its hook had nobody to tell."""
    harness = run_of(tmp_path, processes)
    await hook(harness)
    write(
        TERMINAL_PID,
        record(
            pid=TERMINAL_PID, sessionId="moved-again", procStart=STARTED, updatedAt=NOW + MINUTE
        ),
    )

    assert (await harness.attach("started-in")).session_id == "moved-again"
    harness.close()


async def test_a_hook_heard_after_the_record_was_written_names_the_session(
    tmp_path: Path, processes: Processes
) -> None:
    harness = run_of(tmp_path, processes)
    await hook(harness)
    write(
        TERMINAL_PID,
        record(pid=TERMINAL_PID, sessionId="record-1", procStart=STARTED, updatedAt=NOW - MINUTE),
    )

    assert (await harness.attach("started-in")).session_id == MOVED
    harness.close()


async def test_a_record_of_no_known_time_is_older_than_a_hook(
    tmp_path: Path, processes: Processes
) -> None:
    harness = run_of(tmp_path, processes)
    await hook(harness)
    payload = record(pid=TERMINAL_PID, sessionId="record-1", procStart=STARTED)
    del payload["updatedAt"]
    write(TERMINAL_PID, payload)

    assert (await harness.attach("started-in")).session_id == MOVED
    harness.close()


async def test_a_newer_record_of_another_process_does_not_outrank_the_hook(
    tmp_path: Path, processes: Processes
) -> None:
    harness = run_of(tmp_path, processes)
    await hook(harness)
    write(
        TERMINAL_PID,
        record(pid=TERMINAL_PID, sessionId="record-1", procStart=LATER, updatedAt=NOW + MINUTE),
    )

    assert (await harness.attach("started-in")).session_id == MOVED
    harness.close()


async def test_a_newer_record_that_agrees_with_the_hook_changes_nothing(
    tmp_path: Path, processes: Processes
) -> None:
    harness = run_of(tmp_path, processes)
    await hook(harness, transcript_path="/t/moved.jsonl")
    write(
        TERMINAL_PID,
        record(pid=TERMINAL_PID, sessionId=MOVED, procStart=STARTED, updatedAt=NOW + MINUTE),
    )

    assert (await harness.attach("started-in")).session_id == MOVED
    assert harness.hub.entry(MOVED).transcript == "/t/moved.jsonl", "the hook's detail is kept"
    harness.close()


async def test_a_record_that_names_the_session_the_bridge_names_changes_nothing(
    tmp_path: Path, processes: Processes
) -> None:
    write(TERMINAL_PID, record(pid=TERMINAL_PID, sessionId="started-in", procStart=STARTED))
    harness = run_of(tmp_path, processes)
    attachment = await harness.attach("started-in")
    assert attachment.session_id == "started-in"
    assert list(harness.hub.entries) == ["started-in"]
    harness.close()


# --------------------------------------------------- when the CLI is gone


async def test_a_cli_that_is_gone_is_forgotten_and_one_that_is_not_is_kept(
    tmp_path: Path, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = run_of(tmp_path, processes)
    attachment = await harness.attach("started-in")
    await hook(harness)

    # The bridge closed, as one does when Claude Code reloads its MCP servers; the CLI lives on.
    async def alive(entry: object) -> str:
        return "terminal"

    monkeypatch.setattr(harness.hub, "_holder_control", alive)
    await harness.hub.attach_closed(attachment)
    assert harness.hub.bindings.get(TERMINAL_PID) is not None
    assert (await harness.attach("started-in")).session_id == MOVED

    # The next one closes because the CLI exited, and its pid may be handed to another process.
    no_holder(harness, monkeypatch)
    await harness.hub.attach_closed(harness.hub.entry(MOVED).shared.attachment)  # type: ignore[union-attr]
    assert harness.hub.bindings.get(TERMINAL_PID) is None
    assert json.loads(harness.registry.get_kv(KEY) or "[]") == []
    harness.close()


# ------------------------------------------- the terminal scan, before a bridge dials back


async def scan_after_a_restart(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holder: Holder
) -> None:
    """The first terminal scan of a run, which finds one CLI and the transcript of MOVED."""
    path = tmp_path / f"{MOVED}.jsonl"
    path.write_text("", encoding="utf-8")
    info = transcripts.TranscriptInfo(
        session_id=MOVED, path=str(path), cwd="/repo", size=0, mtime=path.stat().st_mtime
    )
    monkeypatch.setattr(transcripts, "discover", lambda *args: [info])
    monkeypatch.setattr(rollouts, "discover", lambda *args: [])

    async def scan() -> HolderScan:
        return HolderScan(holders=[holder], complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", scan)
    await MirrorService(harness.hub).scan_once()


def a_cli_that_names_no_session(started: str) -> Holder:
    return Holder(
        pid=TERMINAL_PID,
        identity=(TERMINAL_PID, started),
        session_id=None,
        cwd="/elsewhere",
        attachable=True,
    )


async def test_a_session_whose_cli_the_hook_named_is_held_before_its_bridge_dials_back(
    tmp_path: Path, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shim-started CLI names no session in its argv, and nothing else says which one it is in."""
    before = run_of(tmp_path, processes)
    before.seed(MOVED)
    await hook(before)
    before.close()

    after = run_of(tmp_path, processes)
    await scan_after_a_restart(after, tmp_path, monkeypatch, a_cli_that_names_no_session(STARTED))

    entry = after.hub.entry(MOVED)
    assert entry.session.control == "terminal"
    assert (entry.holder_pid, entry.holder_identity) == (TERMINAL_PID, (TERMINAL_PID, STARTED))
    after.close()


async def test_a_process_that_started_later_is_not_the_cli_the_hook_named(
    tmp_path: Path, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = run_of(tmp_path, processes)
    before.seed(MOVED)
    await hook(before)
    before.close()

    after = run_of(tmp_path, processes)
    await scan_after_a_restart(after, tmp_path, monkeypatch, a_cli_that_names_no_session(LATER))

    assert after.hub.entry(MOVED).session.control == "none"
    after.close()


# ------------------------------------------ the scan, with the hook set against it


def heard(tmp_path: Path, session_id: str = "hooked-1", process: str | None = STARTED) -> Harness:
    """A map that heard one hook about the CLI at `TERMINAL_PID`, at `NOW`."""
    harness = Harness(tmp_path)
    harness.hub.bindings.remember(
        SessionStart(session_id, "/repo", TERMINAL_PID, "resume", ""), process
    )
    return harness


def found(session_id: str | None, recorded_at: int = 0, started: str = STARTED) -> HolderScan:
    holder = Holder(
        pid=TERMINAL_PID,
        identity=(TERMINAL_PID, started),
        session_id=session_id,
        cwd="/repo",
        attachable=True,
        recorded_at=recorded_at,
    )
    return HolderScan(holders=[holder], complete=False)


def named(scan: HolderScan) -> list[str | None]:
    return [holder.session_id for holder in scan.holders]


def test_a_cli_the_scan_could_not_name_is_named_by_its_hook(tmp_path: Path) -> None:
    harness = heard(tmp_path)
    assert named(harness.hub.bindings.refine(found(None))) == ["hooked-1"]
    harness.close()


def test_the_session_a_command_line_resumed_gives_way_to_a_later_hook(tmp_path: Path) -> None:
    harness = heard(tmp_path)
    assert named(harness.hub.bindings.refine(found("resumed-1"))) == ["hooked-1"]
    harness.close()


def test_a_record_written_before_the_hook_was_heard_gives_way_to_it(tmp_path: Path) -> None:
    harness = heard(tmp_path)
    assert named(harness.hub.bindings.refine(found("record-1", NOW - MINUTE))) == ["hooked-1"]
    harness.close()


def test_a_record_written_after_the_hook_was_heard_stands(tmp_path: Path) -> None:
    harness = heard(tmp_path)
    assert named(harness.hub.bindings.refine(found("record-1", NOW + MINUTE))) == ["record-1"]
    harness.close()


def test_a_hook_and_a_record_that_agree_leave_the_scan_alone(tmp_path: Path) -> None:
    harness = heard(tmp_path)
    scan = found("hooked-1", NOW + MINUTE)
    refined = harness.hub.bindings.refine(scan)
    assert refined.holders == scan.holders
    harness.close()


def test_a_process_that_started_later_is_not_the_one_the_hook_spoke_of(tmp_path: Path) -> None:
    harness = heard(tmp_path)
    assert named(harness.hub.bindings.refine(found("resumed-1", started=LATER))) == ["resumed-1"]
    harness.close()


def test_a_hook_heard_without_a_start_time_names_the_process_at_its_pid(tmp_path: Path) -> None:
    harness = heard(tmp_path, process=None)
    assert named(harness.hub.bindings.refine(found("resumed-1"))) == ["hooked-1"]
    harness.close()


def test_a_cli_no_hook_spoke_of_is_left_as_the_scan_found_it(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert named(harness.hub.bindings.refine(found("resumed-1"))) == ["resumed-1"]
    harness.close()


def test_refining_changes_no_holder_in_place_and_keeps_whether_the_scan_was_whole(
    tmp_path: Path,
) -> None:
    harness = heard(tmp_path)
    scan = found("resumed-1")
    refined = harness.hub.bindings.refine(scan)
    assert named(scan) == ["resumed-1"]
    assert refined.complete is False
    harness.close()


async def test_a_later_record_decides_which_session_a_scan_finds_held(
    tmp_path: Path, processes: Processes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror's pass, end to end: the hook says one session and a newer record another."""
    before = run_of(tmp_path, processes)
    before.seed(MOVED)
    # Something was said in it, so the sweep does not take it for a session nobody ever used.
    hooked = before.seed("hooked-1")
    hooked.channel.start()
    await said(hooked, "where were we")
    before.hub.bindings.remember(
        SessionStart("hooked-1", "/repo", TERMINAL_PID, "resume", ""), STARTED
    )
    before.close()

    after = run_of(tmp_path, processes)
    holder = Holder(
        pid=TERMINAL_PID,
        identity=(TERMINAL_PID, STARTED),
        session_id=MOVED,
        cwd="/elsewhere",
        attachable=True,
        recorded_at=NOW + MINUTE,
    )
    await scan_after_a_restart(after, tmp_path, monkeypatch, holder)

    assert after.hub.entry(MOVED).session.control == "terminal"
    assert after.hub.entry("hooked-1").session.control == "none"
    after.close()
