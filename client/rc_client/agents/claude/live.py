"""Claude Code's own record of a running CLI: `<claude home>/sessions/<pid>.json`.

Claude Code writes one small file per interactive process and keeps its `sessionId` current across
`/resume`, `/clear` and compaction. That is the account of which session a terminal is in that the
channel bridge cannot give, because its environment is fixed when the CLI starts, and that a
`SessionStart` hook cannot give when it fired while the daemon was down. The device sets it against
what the hook said, by when each was said: the later word names the session.

The format is Claude Code's and is not documented, so a file that does not look as expected says
nothing, and fields this module does not read are ignored.

A record belongs to the process it was written for, and its pid may be somebody else's by now.
`procStart` is that process's start time as `ps -o lstart=` prints it in UTC and the C locale,
which is how `procscan` reports every start time on macOS. Elsewhere the two are written
differently, nothing matches, and no record is believed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import runtime

MAX_RECORD_BYTES = 64 * 1024
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


@dataclass(frozen=True, slots=True)
class LiveRecord:
    """What a record says: the process, the session it is in, and when the process started.

    `updated_at` is when Claude Code last wrote the record, in milliseconds since the epoch, or
    0 when the record does not say. What the record names is the session the process was in then.
    """

    pid: int
    session_id: str
    started: str
    updated_at: int = 0


def sessions_dir() -> Path:
    return runtime.CLAUDE_HOME / "sessions"


def read_records() -> dict[int, LiveRecord]:
    """Every record in Claude Code's sessions folder that names a session, by pid."""
    try:
        paths = sorted(sessions_dir().glob("*.json"))
    except OSError:
        return {}
    records: dict[int, LiveRecord] = {}
    for path in paths:
        record = _parse(path)
        if record is not None:
            records[record.pid] = record
    return records


def verified(records: Mapping[int, LiveRecord], pid: int, started: str | None) -> LiveRecord | None:
    """The record of this process, when it is the process's own.

    `started` is the process's start time as `procscan` reports it; a process whose start time
    cannot be read has no record to check against, so it has none from here.
    """
    record = records.get(pid)
    if record is None or started is None:
        return None
    return record if record.started == _plain(started) else None


def _parse(path: Path) -> LiveRecord | None:
    # A regular file only: opening a pipe left under a record's name would wait for a writer.
    if not path.stem.isdigit() or not path.is_file():
        return None
    pid = int(path.stem)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECORD_BYTES + 1)
        payload = json.loads(raw) if len(raw) <= MAX_RECORD_BYTES else None
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("pid", pid) != pid:
        return None
    session_id, started = payload.get("sessionId"), payload.get("procStart")
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    if not isinstance(started, str) or not started.strip():
        return None
    updated = payload.get("updatedAt")
    return LiveRecord(
        pid=pid,
        session_id=session_id,
        started=_plain(started),
        updated_at=updated if isinstance(updated, int) and not isinstance(updated, bool) else 0,
    )


def _plain(started: str) -> str:
    """The start time with its spacing made one: `ps` pads a single-digit day with a space."""
    return " ".join(started.split())
