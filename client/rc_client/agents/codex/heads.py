"""What the first record of each Codex rollout says, read once per file.

A rollout opens with its thread's `session_meta`, written when the thread starts
and never again: the thread's id, its working directory, and the originator and
source that say whose thread it is (A18, A59). Every scan looks at the rollouts
of the last two weeks, and an automation leaves thousands of `codex exec` ones
behind, each opening with a record of about 20 KB, so reading them all again on
every scan took minutes on a loaded machine. A file is known by its inode, so a
rollout that replaced another under the same name is read afresh. The scans run
on worker threads, so the cache is locked.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from .provenance import owned_here


@dataclass(frozen=True, slots=True)
class Head:
    thread_id: str
    cwd: str
    owned: bool


def session_meta(path: str) -> dict[str, Any] | None:
    """The `session_meta` payload a rollout opens with, or None when it has none yet."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        row = json.loads(first)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict) or row.get("type") != "session_meta":
        return None
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else None


def read_head(path: str) -> Head | None:
    meta = session_meta(path)
    if meta is None:
        return None
    return Head(
        thread_id=str(meta.get("session_id") or meta.get("id") or ""),
        cwd=str(meta.get("cwd") or ""),
        owned=owned_here(meta.get("originator"), meta.get("source")),
    )


class Heads:
    """The heads read so far, by path. One that could not be read is asked again next time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._known: dict[str, tuple[int, Head]] = {}

    def read(self, path: str, inode: int) -> Head | None:
        with self._lock:
            known = self._known.get(path)
        if known is not None and known[0] == inode:
            return known[1]
        head = read_head(path)
        if head is not None:
            with self._lock:
                self._known[path] = (inode, head)
        return head

    def keep(self, paths: set[str]) -> None:
        """Forget every rollout but these, the ones the scan still looks at."""
        with self._lock:
            self._known = {path: known for path, known in self._known.items() if path in paths}


heads = Heads()
