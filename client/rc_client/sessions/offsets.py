"""Where the mirror is in each file it reads, kept in the registry.

The mark is the mirror's, but the hub moves it as well: a session a terminal
started that the device then drove has its rows published by the runner, so
once the device lets go of it the mirror reads on from the end of what that
runner saw, and nothing reaches the apps twice.
"""

from __future__ import annotations

import os

from ..registry import Registry


def offset_key(session_id: str) -> str:
    return f"mirror-offset:{session_id}"


def mark_end(registry: Registry, session_id: str, path: str | None) -> None:
    """Put the mark at the end of `path`: everything before it is published."""
    size = _size_of(path) if path else None
    if size is not None and registry.get_kv(offset_key(session_id)) != str(size):
        registry.set_kv(offset_key(session_id), str(size))


def _size_of(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except (OSError, ValueError):
        return None
