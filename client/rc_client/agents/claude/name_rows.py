"""The rows that name a Claude Code session, written and read back (A55).

`/rename` leaves two rows in a transcript: `custom-title`, which `/resume` lists
and `claude --resume <name>` matches, and `agent-name`, which the prompt bar
shows after a resume. A session no process holds is renamed by writing the same
two rows. While a CLI holds the session only `custom-title` is ours to write:
the CLI absorbs it when it next reads the end of its transcript, but it
re-stamps the `agent-name` it has cached after ours, so one written under it is
lost. Both are internal to Claude Code, so their shape is a recorded one, not a
promise.

The rows are appended with `O_APPEND` and never `O_CREAT`, one write for both: a
transcript that is not there is a session that never started, not one to start,
and the open fails instead of making it. That is the SDK's own `rename_session`
helper, which writes `custom-title` the same way.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

from . import transcripts


def size_of(path: Path) -> int:
    """How long the transcript is now, which is where later reads start."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def name_since(path: Path, after: int) -> str | None:
    """The name the user last gave the session in the rows written after byte `after`."""
    tail = transcripts.TitleTail(path=str(path))
    tail.tail.offset = after
    names = [title.text for title in tail.read_new() if title.by_user]
    return names[-1] if names else None


def append_names(path: Path, session_id: str, name: str, *, held: bool) -> None:
    """Append `custom-title`, and `agent-name` too unless a CLI `held`s the session.

    With nothing holding the file, one that does not end with a newline, which is
    what a write cut short leaves, gets one first, as the CLI repairs its own
    transcript. A CLI that holds it is the one to do that: a line it is writing
    now is not ours to cut, so the rows go on the end as they are. An empty file
    is a stub, not a session. Every failure is the `OSError` the open or the
    write raised.
    """
    rows = [{"type": transcripts.CUSTOM_TITLE_ROW, "customTitle": name, "sessionId": session_id}]
    if not held:
        rows.append(
            {"type": transcripts.AGENT_NAME_ROW, "agentName": name, "sessionId": session_id}
        )
    data = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
    try:
        if os.fstat(descriptor).st_size == 0:
            raise FileNotFoundError(errno.ENOENT, "an empty transcript is not a session", str(path))
        if not held and not _ends_with_newline(path):
            data = b"\n" + data
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
    finally:
        os.close(descriptor)


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"
