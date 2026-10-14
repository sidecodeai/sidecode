"""Whether any subagent a Claude session started is still working.

The rule of `docs/DESIGN.md` § "Working means all of it": a
session is working while any work it started is still under way — its own turn,
or a subagent it spawned that has not finished — and the turn does not end until
the last of it has. Claude Code offers nothing to ask: a subagent's metadata
carries what it is and who called it, never how it is doing. What it does write
is every subagent's transcript, under the session's own directory, so the last
row of each file is the whole answer.

A subagent is working until its transcript ends with an assistant message that
ended a turn (`end_turn`, `stop_sequence`, `max_tokens`) or with the API error
that stopped it. That is deliberately not the rule the parent's own transcript
uses, where a missing `stop_reason` ends the turn as well (A34): the parent is
read row by row as it grows, while these files are read from the end long after
the fact, and a large share of subagent transcripts stop on an
assistant row whose message never completed. Reading those as finished is the
defect this file exists to fix. Nothing marks a subagent that was killed, so a
transcript nothing has written to for thirty minutes counts as abandoned, which
is what stops one from holding a session green for good.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Where the CLI files them: `<project>/<session id>/subagents/agent-<id>.jsonl`,
# beside the session's own `<project>/<session id>.jsonl`.
SUBAGENT_DIR = "subagents"
SUBAGENT_GLOB = "agent-*.jsonl"

# How long a transcript may go unwritten before its subagent counts as gone.
SUBAGENT_STALE_S = 1800.0

# How much of a transcript's tail is read to find its last row. A row longer
# than this reads as "not finished", which is the safe direction: the session
# stays green and the bound above is what ends it.
TAIL_BYTES = 64 * 1024

# What the CLI writes where a subagent's turn is over.
ENDED_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})


def subagents_dir(transcript: str | Path) -> Path:
    """The directory holding one session's subagent transcripts."""
    path = Path(transcript)
    return path.parent / path.stem / SUBAGENT_DIR


def transcript_ended(last_row: Any) -> bool:
    """Whether the last row of a subagent's transcript says it has finished.

    Anything else is work in progress: a pending `tool_use`, the user row
    carrying a tool result, a streamed block whose message never completed, an
    empty file, a row that is not JSON at all.
    """
    if not isinstance(last_row, dict):
        return False
    if last_row.get("isApiErrorMessage"):
        return True
    if last_row.get("type") != "assistant":
        return False
    message = last_row.get("message")
    stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
    return isinstance(stop_reason, str) and stop_reason in ENDED_STOP_REASONS


def read_last_row(path: str | Path, tail_bytes: int = TAIL_BYTES) -> dict[str, Any] | None:
    """The last JSON object in a file, read from its end and nowhere else.

    Seeking into the middle of a row leaves a fragment at the front of the
    buffer, which is why the search runs backwards and skips whatever does not
    parse: the fragment is only ever reached when nothing after it parsed
    either, and it does not parse.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            data = handle.read()
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            return row
    return None


@dataclass(slots=True)
class SubagentWatch:
    """One session's subagents, cheap enough to ask on every tail interval.

    A transcript that has finished is remembered by its `(mtime, size)` and
    never read again, so a session that spawned a hundred subagents costs one
    `stat` each and a read only of the few still running.
    """

    directory: Path
    stale_s: float = SUBAGENT_STALE_S
    tail_bytes: int = TAIL_BYTES
    _ended: dict[str, tuple[float, int]] = field(default_factory=dict, init=False)

    @classmethod
    def for_transcript(cls, transcript: str | Path, **options: Any) -> SubagentWatch:
        return cls(directory=subagents_dir(transcript), **options)

    def working(self) -> bool:
        """Whether any subagent of this session is still under way."""
        return bool(self.working_agents())

    def working_agents(self) -> list[str]:
        """Which of them are, by file name, for logging and for tests."""
        now = time.time()
        ended: dict[str, tuple[float, int]] = {}
        found: list[str] = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue
            key = str(path)
            mark = (stat.st_mtime, stat.st_size)
            if now - stat.st_mtime > self.stale_s:
                continue
            if self._ended.get(key) == mark:
                ended[key] = mark
                continue
            if transcript_ended(read_last_row(path, self.tail_bytes)):
                ended[key] = mark
                continue
            found.append(path.stem)
        # Rebuilt rather than updated, so a session whose directory was removed
        # does not keep one entry per transcript it ever had.
        self._ended = ended
        return found

    def _files(self) -> list[Path]:
        try:
            return sorted(self.directory.glob(SUBAGENT_GLOB))
        except OSError:
            return []
