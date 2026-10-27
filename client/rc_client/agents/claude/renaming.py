"""Amendment A55: renaming a Claude Code session, by the route its state allows.

- **Driven** (`control: remote`, runner alive): the device asks the CLI, with the
  SDK's `rename_session` control request, and reads back the name it wrote. If the
  request fails or is not there to send, it writes the `custom-title` row itself.
- **Attached to a terminal it can type into**: it types `/rename <name>` under A40's
  idle gate and waits for the CLI's `custom-title` row. A name the prompt box would
  misread (`typeable`) is written as that row instead, and nothing is typed.
- **Open in a terminal it cannot reach**: it refuses, `unsupported`.
- **Held by nothing**: it writes `custom-title` and `agent-name`.

The row, not the CLI's wording, is what says a rename took, and the name it
carries is the one the session now has: the CLI suffixes a name another live
session on the machine holds. Writing a row is the SDK helper's own route
(`name_rows.py`), safe under a running CLI for `custom-title` and not for
`agent-name`, which it re-stamps from its cache.
"""

from __future__ import annotations

import asyncio
import time

from ...errors import RcError
from ...logging_setup import logger
from ...sessions.typist import NOT_TYPEABLE, typeable
from ..registry import RenameSpec
from . import name_rows, transcripts
from .adapter import ClaudeRunner

log = logger("rc_client.claude")

# How long, and how often, the transcript is looked at after the CLI took a
# rename request: the row is written a moment after the answer.
READBACK = 2.0
READBACK_POLL = 0.1

NO_TRANSCRIPT = "the session has no transcript to hold a name yet"
UNWRITABLE = "the session's transcript cannot be written"


async def rename(spec: RenameSpec) -> str:
    """Rename the session in Claude Code, and return the name it now holds."""
    entry = spec.entry
    if isinstance(entry.runner, ClaudeRunner):
        return await _driven(entry.runner, spec)
    if entry.shared is not None:
        return await _attached(spec)
    if entry.session.control in ("terminal", "shared"):
        raise RcError("unsupported", NOT_TYPEABLE)
    return await _write(spec.session.session_id, spec.name, held=False)


async def _driven(runner: ClaudeRunner, spec: RenameSpec) -> str:
    """Ask the CLI the way a host does; its own routine writes both rows."""
    session_id = spec.session.session_id
    mark = await asyncio.to_thread(_mark, session_id)
    if await runner.rename_session(spec.name):
        written = await _read_back(session_id, mark)
        if written is not None:
            return written
        if await asyncio.to_thread(transcripts.find_transcript, session_id) is None:
            # A session that has not written yet has nothing to look at; the CLI
            # keeps the name until it does.
            return spec.name
    # The request failed, was not there to send, or left no row: the row is ours.
    return await _write(session_id, spec.name, held=True)


async def _attached(spec: RenameSpec) -> str:
    """Type the command, unless the name is one the prompt box would misread."""
    if spec.shared.link_for(spec.entry) is None:
        raise RcError("unsupported", NOT_TYPEABLE)
    if typeable(spec.name):
        return await spec.shared.rename(spec.entry, spec.name)
    return await _write(spec.session.session_id, spec.name, held=True)


async def _write(session_id: str, name: str, *, held: bool) -> str:
    path = await asyncio.to_thread(transcripts.find_transcript, session_id)
    if path is None:
        raise RcError("unsupported", NO_TRANSCRIPT)
    try:
        await asyncio.to_thread(name_rows.append_names, path, session_id, name, held=held)
    except FileNotFoundError as exc:
        raise RcError("unsupported", NO_TRANSCRIPT) from exc
    except OSError as exc:
        log.warning("writing a session's name failed", error=type(exc).__name__)
        raise RcError("unsupported", UNWRITABLE) from exc
    return name


def _mark(session_id: str) -> int:
    path = transcripts.find_transcript(session_id)
    return name_rows.size_of(path) if path is not None else 0


def _written_since(session_id: str, mark: int) -> str | None:
    path = transcripts.find_transcript(session_id)
    return name_rows.name_since(path, mark) if path is not None else None


async def _read_back(session_id: str, mark: int) -> str | None:
    """The name the CLI wrote since `mark`, waiting a moment for the row to land."""
    deadline = time.monotonic() + READBACK
    while True:
        written = await asyncio.to_thread(_written_since, session_id, mark)
        if written is not None or time.monotonic() >= deadline:
            return written
        await asyncio.sleep(READBACK_POLL)
