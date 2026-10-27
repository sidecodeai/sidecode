"""Whether any subagent a Claude session started is still working.

The rule of `docs/DESIGN.md` § "Working means all of it": a
session is working while any work it started is still under way — its own turn,
or a subagent it spawned that has not finished — and the turn does not end until
the last of it has. Claude Code offers nothing to ask: a subagent's metadata
carries what it is and who called it, never how it is doing. What it does write
is every subagent's transcript, under the session's own directory, so the last
row of each file is the whole answer.

A subagent is working until its transcript ends with an assistant message that
ended a turn (`end_turn`, `stop_sequence`, `max_tokens`), with the API error
that stopped it, or with the marker the CLI leaves where the person's Escape
aborted it. That is deliberately not the rule the parent's own transcript
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

from .calls import Call, read_calls
from .markers import is_interruption

# Where the CLI files them: `<project>/<session id>/subagents/agent-<id>.jsonl`,
# beside the session's own `<project>/<session id>.jsonl`; a workflow's agents file theirs a
# level deeper, under `workflows/<run>/`.
SUBAGENT_DIR = "subagents"
AGENT_PREFIX = "agent-"
SUBAGENT_GLOB = f"{AGENT_PREFIX}*.jsonl"

# How long a transcript may go unwritten before its subagent counts as gone.
SUBAGENT_STALE_S = 1800.0

# How much of a transcript's tail is read to find its last row. A row longer
# than this reads as "not finished", which is the safe direction: the session
# stays green and the bound above is what ends it.
TAIL_BYTES = 64 * 1024

# What the CLI writes where a subagent's turn is over.
ENDED_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})

# How many transcripts are read for their tool calls at most, the newest first.
MAX_CALL_FILES = 32


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
    if last_row.get("type") == "user":
        return _interrupted(last_row)
    if last_row.get("type") != "assistant":
        return False
    message = last_row.get("message")
    stop_reason = message.get("stop_reason") if isinstance(message, dict) else None
    return isinstance(stop_reason, str) and stop_reason in ENDED_STOP_REASONS


def _interrupted(row: dict[str, Any]) -> bool:
    """Whether a user row holds the marker left where the person's Escape stopped the turn.

    The marker is text, and rides in the row that also carries the result of
    the tool it interrupted, so the text is matched and the row is not.
    """
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return is_interruption(content)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and is_interruption(str(block.get("text") or ""))
        for block in content
    )


def read_last_row(path: str | Path, tail_bytes: int = TAIL_BYTES) -> dict[str, Any] | None:
    """The last JSON object in a file, read from its end and nowhere else.

    Seeking into the middle of a row leaves a fragment at the front of the
    buffer, which is why the search runs backwards and skips whatever does not
    parse: the fragment is only ever reached when nothing after it parsed
    either, and it does not parse. A fragment that begins inside a multibyte
    character is not even text, and fails as `UnicodeDecodeError` rather than
    `JSONDecodeError`, so both are skipped.
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
        except ValueError:
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
    _calls: dict[str, tuple[float, int, dict[str, Call]]] = field(default_factory=dict, init=False)

    @classmethod
    def for_transcript(cls, transcript: str | Path, **options: Any) -> SubagentWatch:
        return cls(directory=subagents_dir(transcript), **options)

    def working(self) -> bool:
        """Whether any subagent of this session is still under way."""
        return bool(self.working_agents())

    def activity(self) -> dict[str, float]:
        """When each subagent's transcript was last written, by agent id, while it is recent.

        A write is a sign of life, whatever its last row says: it is how a
        subagent that works for hours is told from one whose end was lost.
        """
        now = time.time()
        written: dict[str, float] = {}
        for path in self._files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime <= self.stale_s:
                agent_id = path.stem.removeprefix(AGENT_PREFIX)
                written[agent_id] = max(mtime, written.get(agent_id, 0.0))
        return written

    def calls(self, since: float) -> dict[str, Call]:
        """The tool calls in the tails of the transcripts written since `since`, by call id.

        What a subagent is asked at a permission prompt is in its own transcript
        and nowhere else, so this is read only while a prompt or a question
        waits (`agents/claude/calls.py`). A transcript that has not grown since
        it was last read is not read again.
        """
        listed: list[tuple[float, int, Path]] = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= since:
                listed.append((stat.st_mtime, stat.st_size, path))
        found: dict[str, Call] = {}
        read: dict[str, tuple[float, int, dict[str, Call]]] = {}
        for mtime, size, path in sorted(listed, reverse=True)[:MAX_CALL_FILES]:
            key = str(path)
            known = self._calls.get(key)
            calls = (
                known[2] if known is not None and known[:2] == (mtime, size) else read_calls(path)
            )
            read[key] = (mtime, size, calls)
            found.update(calls)
        self._calls = read
        return found

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
        """Every subagent transcript, those of a workflow's agents a level deeper included."""
        try:
            return sorted(self.directory.rglob(SUBAGENT_GLOB))
        except OSError:
            return []
