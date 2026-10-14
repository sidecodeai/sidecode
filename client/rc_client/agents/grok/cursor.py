"""The `eventId` counter that decides what a Grok session has already published.

Grok stamps every conversation row with `_meta.eventId` of the form
`<sessionId>-<n>`, the same counter it writes into `updates.jsonl`. One device
reads those rows from two places — the mirror tailing the log, and the leader
replaying a session it just joined (A28) — so both remember the highest `n` they
applied under one registry key. A replay below that mark is history the apps
already have; anything above it is news.
"""

from __future__ import annotations

from typing import Any

from ...registry import Registry

PREFIX = "grok-cursor:"


def key(session_id: str) -> str:
    return f"{PREFIX}{session_id}"


def read(registry: Registry, session_id: str) -> int:
    stored = registry.get_kv(key(session_id))
    return int(stored) if stored is not None and stored.isdigit() else 0


def write(registry: Registry, session_id: str, index: int) -> None:
    registry.set_kv(key(session_id), str(index))


def index_of(params: dict[str, Any]) -> int:
    """The `n` of a notification's `<sessionId>-<n>` event id, or 0 without one."""
    meta = params.get("_meta")
    event_id = meta.get("eventId") if isinstance(meta, dict) else None
    if not isinstance(event_id, str) or "-" not in event_id:
        return 0
    tail = event_id.rsplit("-", 1)[1]
    return int(tail) if tail.isdigit() else 0


def is_replay(params: dict[str, Any]) -> bool:
    """Whether this row is part of the history `session/load` replays."""
    meta = params.get("_meta")
    if isinstance(meta, dict) and meta.get("isReplay"):
        return True
    update = params.get("update")
    return isinstance(update, dict) and bool(update.get("isReplay"))
