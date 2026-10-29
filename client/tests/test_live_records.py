"""Claude Code's own record of a live CLI, and what the device takes from it.

Claude Code keeps `<claude home>/sessions/<pid>.json` current across `/resume`, `/clear` and
compaction. Its format is not documented, so the reader is tolerant, and a record is believed only
for the process it was written for. The values in the fixture are made up; its keys are the ones
Claude Code 2.1.311 writes.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from rc_client import procscan
from rc_client.agents.claude import holders, live, runtime
from rc_client.procscan import Proc

FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "live-record.json"
PID = 41001
SESSION = "6f1d2c3a-0b4e-4f5a-9c7d-1a2b3c4d5e6f"
STARTED = "Tue Oct 27 14:39:20 2026"


def record(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(changes)
    return payload


def write(pid: int, payload: dict[str, Any] | str) -> Path:
    directory = live.sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


# ------------------------------------------------------------------ the reader


def test_a_record_names_the_session_its_process_is_in() -> None:
    write(PID, record())
    found = live.read_records()
    assert found == {
        PID: live.LiveRecord(pid=PID, session_id=SESSION, started=STARTED, updated_at=1793112062214)
    }


def test_the_records_live_in_the_claude_home_the_rest_of_the_client_uses() -> None:
    assert live.sessions_dir() == runtime.CLAUDE_HOME / "sessions"


def test_a_folder_that_is_not_there_has_no_records() -> None:
    assert live.read_records() == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[1, 2, 3]",
        "",
        "{}",
        json.dumps(record(sessionId=7)),
        json.dumps(record(sessionId="")),
        json.dumps(record(sessionId="has a space")),
        json.dumps(record(sessionId="x" * 200)),
        json.dumps({key: value for key, value in record().items() if key != "sessionId"}),
        json.dumps({key: value for key, value in record().items() if key != "procStart"}),
        json.dumps(record(procStart="")),
        json.dumps(record(procStart=1793111962)),
        # Written for another process than the file's name says.
        json.dumps(record(pid=PID + 1)),
    ],
)
def test_a_record_that_does_not_look_as_expected_says_nothing(payload: str) -> None:
    write(PID, payload)
    assert live.read_records() == {}


def test_what_the_reader_does_not_know_is_ignored() -> None:
    write(PID, record(someFutureField={"nested": [1, 2]}, status=None, kind=3))
    assert list(live.read_records()) == [PID]


def test_a_record_without_a_pid_field_is_taken_by_its_file_name() -> None:
    payload = record()
    del payload["pid"]
    write(PID, payload)
    assert list(live.read_records()) == [PID]


def test_only_files_named_for_a_pid_are_read() -> None:
    write(PID, record())
    directory = live.sessions_dir()
    (directory / "notes.json").write_text(json.dumps(record()), encoding="utf-8")
    (directory / f"{PID}.fa342a861059.key").write_text("secret", encoding="utf-8")
    assert list(live.read_records()) == [PID]


def test_a_pipe_under_a_records_name_is_not_opened() -> None:
    """Opening it would wait for a writer, and the terminal scan would wait with it."""
    live.sessions_dir().mkdir(parents=True)
    pipe = live.sessions_dir() / f"{PID}.json"
    os.mkfifo(pipe)
    found: list[dict[int, live.LiveRecord]] = []
    reader = threading.Thread(target=lambda: found.append(live.read_records()), daemon=True)
    reader.start()
    reader.join(timeout=2)
    try:
        assert not reader.is_alive(), "the reader is stuck opening the pipe"
        assert found == [{}]
    finally:
        if reader.is_alive():
            # Let the stuck open finish, so no thread is left behind it.
            with contextlib.suppress(OSError):
                os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
            reader.join(timeout=2)


def test_a_record_past_the_size_bound_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    write(PID, record())
    monkeypatch.setattr(live, "MAX_RECORD_BYTES", 100)
    assert live.read_records() == {}


def test_a_single_digit_day_is_spaced_the_way_ps_spaces_it() -> None:
    write(PID, record(procStart="Wed Oct  7 09:05:01 2026"))
    assert live.read_records()[PID].started == "Wed Oct 7 09:05:01 2026"


# ------------------------------------------------- belief: the process's own record


def test_a_record_is_believed_for_the_process_it_was_written_for() -> None:
    write(PID, record())
    records = live.read_records()
    found = live.verified(records, PID, STARTED)
    assert found is not None and found.session_id == SESSION
    # `ps` pads a single-digit day, the record may not, and the spacing is not the point.
    write(PID + 1, record(pid=PID + 1, procStart="Wed Oct 7 09:05:01 2026"))
    padded = live.verified(live.read_records(), PID + 1, "Wed Oct  7 09:05:01 2026")
    assert padded is not None and padded.session_id == SESSION


def test_a_pid_that_a_later_process_was_given_is_not_the_one_the_record_describes() -> None:
    write(PID, record())
    records = live.read_records()
    assert live.verified(records, PID, "Wed Oct 28 01:02:03 2026") is None


def test_a_process_whose_start_cannot_be_read_has_no_record_from_here() -> None:
    write(PID, record())
    assert live.verified(live.read_records(), PID, None) is None


def test_a_pid_with_no_record_has_none() -> None:
    assert live.verified({}, PID, STARTED) is None


# ------------------------------------------------------ when it was last written


def test_a_record_says_when_claude_code_last_wrote_it() -> None:
    write(PID, record())
    assert live.read_records()[PID].updated_at == 1793112062214


@pytest.mark.parametrize("updated", ["soon", True, 1.5, None, [1]])
def test_a_time_that_is_not_a_whole_number_is_no_time(updated: Any) -> None:
    write(PID, record(updatedAt=updated))
    assert live.read_records()[PID].updated_at == 0


def test_a_record_without_a_time_is_still_a_record() -> None:
    payload = record()
    del payload["updatedAt"]
    write(PID, payload)
    found = live.read_records()[PID]
    assert (found.session_id, found.updated_at) == (SESSION, 0)


# ---------------------------------------------------- the holder scan reads it


def fake_processes(monkeypatch: pytest.MonkeyPatch, procs: list[Proc]) -> None:
    async def scan() -> procscan.Scan:
        return procscan.Scan(procs=procs, complete=True)

    async def cwds(pids: list[int]) -> tuple[dict[int, str], bool]:
        return {pid: "/repo" for pid in pids}, True

    monkeypatch.setattr(holders, "scan_processes", scan)
    monkeypatch.setattr(holders, "process_cwds", cwds)


def claude(pid: int, command: str = "/usr/local/bin/claude", start: str = STARTED) -> Proc:
    return Proc(pid=pid, ppid=1, start=start, command=command)


async def test_a_cli_started_without_a_resume_flag_is_named_by_its_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(PID, record())
    fake_processes(monkeypatch, [claude(PID)])
    scan = await holders.scan_holders()
    assert [(holder.pid, holder.session_id) for holder in scan.holders] == [(PID, SESSION)]
    # So the session it is in has a holder, whatever directory it runs in.
    found = scan.assign([holders.SessionRef(SESSION, cwd="/elsewhere")])
    assert found[SESSION].pid == PID


async def test_a_holder_says_when_the_record_that_named_it_was_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(PID, record())
    fake_processes(monkeypatch, [claude(PID), claude(7, "/usr/local/bin/claude --resume old-1")])
    scan = await holders.scan_holders()
    assert {holder.pid: holder.recorded_at for holder in scan.holders} == {
        PID: 1793112062214,
        7: 0,
    }


async def test_the_record_is_fresher_than_the_session_the_cli_was_resumed_into(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude --resume old`, then `/resume other`: the argv still says `old`."""
    write(PID, record())
    fake_processes(monkeypatch, [claude(PID, "/usr/local/bin/claude --resume old-1")])
    scan = await holders.scan_holders()
    assert scan.holders[0].session_id == SESSION


async def test_a_record_of_another_process_leaves_the_argv_to_name_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write(PID, record())
    fake_processes(
        monkeypatch,
        [claude(PID, "/usr/local/bin/claude --resume old-1", start="Wed Oct 28 01:02:03 2026")],
    )
    scan = await holders.scan_holders()
    assert scan.holders[0].session_id == "old-1"


async def test_without_a_record_the_argv_names_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_processes(monkeypatch, [claude(PID, "/usr/local/bin/claude --resume old-1"), claude(7)])
    scan = await holders.scan_holders()
    assert {holder.pid: holder.session_id for holder in scan.holders} == {PID: "old-1", 7: None}


# ------------------------------------------------- when a process started


async def test_a_process_start_is_asked_of_ps_in_utc_and_the_c_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    async def run(
        *args: str, timeout: float = procscan.SCAN_TIMEOUT, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        seen.append((args, env))
        return 0, "Tue Oct  7 14:39:20 2026    \n"

    monkeypatch.setattr(procscan, "IS_LINUX", False)
    monkeypatch.setattr(procscan, "_run", run)
    assert await procscan.process_start(PID) == "Tue Oct 7 14:39:20 2026"
    assert seen == [(("ps", "-o", "lstart=", "-p", str(PID)), {"TZ": "UTC", "LC_ALL": "C"})]


@pytest.mark.parametrize(("code", "out"), [(1, ""), (0, ""), (127, ""), (124, "")])
async def test_a_process_that_is_gone_or_cannot_be_asked_about_has_no_start(
    monkeypatch: pytest.MonkeyPatch, code: int, out: str
) -> None:
    async def run(
        *args: str, timeout: float = procscan.SCAN_TIMEOUT, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        return code, out

    monkeypatch.setattr(procscan, "IS_LINUX", False)
    monkeypatch.setattr(procscan, "_run", run)
    assert await procscan.process_start(PID) is None


async def test_the_process_table_and_one_process_are_read_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both tell a start time the same way, so a start from one is comparable with the other."""
    seen: list[dict[str, str] | None] = []

    async def run(
        *args: str, timeout: float = procscan.SCAN_TIMEOUT, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        seen.append(env)
        if "-axww" in args:
            return 0, f"{PID} 1 Tue Oct  7 14:39:20 2026 ttys001 claude --resume x\n"
        return 0, "Tue Oct  7 14:39:20 2026\n"

    monkeypatch.setattr(procscan, "IS_LINUX", False)
    monkeypatch.setattr(procscan, "_run", run)
    scan = await procscan.scan_processes()
    one = await procscan.process_start(PID)
    assert seen == [procscan.START_ENV, procscan.START_ENV]
    assert scan.procs[0].start == one == "Tue Oct 7 14:39:20 2026"
