"""Name a pi session that nothing holds, by appending to its file (A55).

pi keeps a session as JSONL under `~/.pi/agent/sessions/--<cwd>--/<timestamp>_<id>.jsonl`:
a header line, `{"type":"session","version":3,"id":...}`, then one entry per line,
each with an 8-hex `id` and the `parentId` it hangs from. A name is a
`session_info` entry. The latest one in the file is the name, and the leaf, where
the next entry attaches, is the last entry in file order. pi renames a stored
session the same way from its own `/resume` selector, so a name written here is
read as if pi had written it.

Only a session no process holds is written to, and only a file already at the
current version: pi migrates an older one when it loads it, and an entry added
before that would not survive. A running pi keeps its leaf in memory, and one
that holds the file would take an entry appended behind its back for a side
branch, so those are renamed through the process instead (`naming.py`).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...errors import RcError
from . import runtime

VERSION = 3
# pi's own rule for a session id. It is also what keeps the id from being a
# pattern when it is looked for by name.
SESSION_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# What pi reads of a header before it gives up on the file.
MAX_HEADER_BYTES = 1024 * 1024
ENTRY_ID_TRIES = 100

MISSING = "the pi session file was not found"
UNREADABLE = "the pi session file cannot be read"
UNWRITABLE = "the pi session file cannot be written"
OLD_FORMAT = "the pi session file is in an older format; open it in pi to rename it"


@dataclass(slots=True)
class Leaf:
    """What the end of a session file says about where the next entry goes."""

    # The id of the last entry in file order, or `None` for a file with only a header.
    parent: str | None
    # Every entry id in the file, so a new one is never a duplicate.
    ids: set[str]
    # Whether the last byte is a newline, which a crash in the middle of a line leaves undone.
    terminated: bool


def name_session(session_id: str, name: str) -> None:
    """Append `name` to the file of the session, or say why it cannot be."""
    path = find(session_id)
    if path is None:
        raise RcError("unsupported", MISSING)
    append_name(path, name)


def find(session_id: str) -> Path | None:
    """The file whose own header says it is this session, the newest if several do."""
    if not SESSION_ID.fullmatch(session_id):
        return None
    candidates: list[tuple[float, Path]] = []
    for path in runtime.sessions_dir().glob(f"*/*_{session_id}.jsonl"):
        header = _header(path)
        if header is None or header.get("id") != session_id:
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(candidates)[1] if candidates else None


def append_name(path: Path, name: str) -> None:
    """Add a `session_info` entry to the end of the file, as pi would.

    The file is opened for appending and never created, and the row goes out in
    one write, so a pi that starts meanwhile interleaves whole lines or none. A
    last line a crash left unfinished gets its newline first, which is what pi
    does itself when it loads the file; the stump stays a line pi skips.
    """
    leaf = _leaf(path)
    entry = {
        "type": "session_info",
        "id": _entry_id(leaf.ids),
        "parentId": leaf.parent,
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "name": name,
    }
    row = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    payload = (("" if leaf.terminated else "\n") + row + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        raise RcError("unsupported", UNWRITABLE) from exc
    try:
        written = os.write(descriptor, payload)
    except OSError as exc:
        raise RcError("unsupported", UNWRITABLE) from exc
    finally:
        os.close(descriptor)
    if written != len(payload):
        raise RcError("unsupported", UNWRITABLE)


def branch(session_id: str, path: str | None) -> list[dict[str, Any]]:
    """The entries on the path the session is on, oldest first, as `getBranch()` returns them.

    A `hello` from the extension names the session file rather than carrying the
    branch, and the device reads it only when it first meets a session. The
    file must be the session's own, under pi's sessions directory; the branch ends
    at the last entry in file order, the leaf, and runs back through `parentId`.
    Anything that cannot be read is an empty branch: the session is then shown
    from its next word on, as before a branch could be replayed at all.
    """
    file = _session_path(session_id, path)
    if file is None:
        return []
    by_id: dict[str, dict[str, Any]] = {}
    leaf: str | None = None
    try:
        with file.open("rb") as handle:
            for raw in handle:
                entry = _parse(raw)
                entry_id = entry.get("id") if entry is not None else None
                if entry is None or entry.get("type") == "session" or not isinstance(entry_id, str):
                    continue
                by_id[entry_id] = entry
                leaf = entry_id
    except OSError:
        return []
    path_back: list[dict[str, Any]] = []
    seen: set[str] = set()
    while leaf is not None and leaf in by_id and leaf not in seen:
        seen.add(leaf)
        entry = by_id[leaf]
        path_back.append(entry)
        parent = entry.get("parentId")
        leaf = parent if isinstance(parent, str) else None
    return list(reversed(path_back))


def _session_path(session_id: str, path: str | None) -> Path | None:
    """The session's own file at `path`, when it is one: inside pi's sessions, headed by its id."""
    if not path:
        return None
    try:
        file = Path(path).resolve()
        root = runtime.sessions_dir().resolve()
    except OSError:
        return None
    if root not in file.parents or not file.is_file():
        return None
    header = _header(file)
    return file if header is not None and header.get("id") == session_id else None


def _header(path: Path) -> dict[str, Any] | None:
    """The first line of the file when it is a session header."""
    try:
        with path.open("rb") as handle:
            first = handle.readline(MAX_HEADER_BYTES)
    except OSError:
        return None
    entry = _parse(first)
    return entry if entry is not None and entry.get("type") == "session" else None


def _leaf(path: Path) -> Leaf:
    """Read the file the way pi does: every line that parses is an entry, the rest are skipped."""
    header: dict[str, Any] | None = None
    parent: str | None = None
    ids: set[str] = set()
    try:
        with path.open("rb") as handle:
            for raw in handle:
                entry = _parse(raw)
                if entry is None:
                    continue
                if header is None:
                    header = entry
                    continue
                if entry.get("type") == "session":
                    continue
                entry_id = entry.get("id")
                if isinstance(entry_id, str):
                    ids.add(entry_id)
                    parent = entry_id
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size:
                handle.seek(size - 1)
            terminated = size == 0 or handle.read(1) == b"\n"
    except OSError as exc:
        raise RcError("unsupported", UNREADABLE) from exc
    if header is None or header.get("type") != "session":
        raise RcError("unsupported", UNREADABLE)
    if header.get("version") != VERSION:
        raise RcError("unsupported", OLD_FORMAT)
    return Leaf(parent, ids, terminated)


def _parse(raw: bytes) -> dict[str, Any] | None:
    """One line as an object, or `None` for a blank or unreadable one."""
    line = raw.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _entry_id(taken: set[str]) -> str:
    """Eight hex digits not yet in the file, as pi makes them; a whole UUID if it must."""
    for _ in range(ENTRY_ID_TRIES):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in taken:
            return candidate
    return str(uuid.uuid4())
