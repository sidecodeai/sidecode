"""Which session each terminal CLI is in, as its `SessionStart` hook last said (A16).

The hook is the only account the device gets of a `/resume`, `/clear` or compaction, and the
channel bridge keeps naming the session its CLI started in. The map lives in the registry as well
as in memory: a restart empties memory, and the CLI that outlives it is still where its hook put it.

An entry names its CLI by pid and start time, so a pid that some later process was given is not
taken for the CLI that had it. An entry made while the start time could not be read is believed
for the life of this process only, and is never stored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, TypeGuard

from ..agents.claude.holders import HolderScan
from ..models import now_ms
from ..registry import Registry
from .attach import SessionStart

KEY = "terminal-bindings"
# How many terminals may be remembered at once: a CLI that never says it has gone must not make
# the map, and the row that holds it, grow without end.
MAX_BINDINGS = 64


@dataclass(frozen=True, slots=True)
class Binding:
    """The session a hook put a CLI in, the start time of the CLI it was said about, and when.

    `at` is when the daemon heard the hook, in milliseconds since the epoch.
    """

    start: SessionStart
    process: str | None
    at: int

    def is_process(self, pid: int, started: str) -> bool:
        """Whether this is the process the hook spoke of: its pid, and its start time when read."""
        return self.start.pid == pid and self.process in (None, started)


class TerminalBindings:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry
        self._bindings: dict[int, Binding] = {}

    def load(self) -> None:
        """Read back what the last run knew; an entry that does not decode is skipped."""
        self._bindings.clear()
        raw = self._registry.get_kv(KEY)
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            return
        for item in items if isinstance(items, list) else []:
            binding = _decode(item)
            if binding is not None:
                self._bindings[binding.start.pid] = binding

    def get(self, pid: int) -> Binding | None:
        return self._bindings.get(pid)

    @staticmethod
    def stamp() -> int:
        """When a hook is heard: taken as it arrives, before anything is awaited for it."""
        return now_ms()

    def remember(self, start: SessionStart, process: str | None, at: int | None = None) -> bool:
        """Keep what a hook said, unless a later hook of the same CLI was heard first.

        Two hooks that arrive close together are each held up by a look at the process, and the
        later can be done first. The one heard earlier then says nothing new: false.
        """
        heard = self.stamp() if at is None else at
        current = self._bindings.get(start.pid)
        if current is not None and current.at > heard:
            return False
        self._bindings.pop(start.pid, None)
        self._bindings[start.pid] = Binding(start, process, heard)
        for pid in list(self._bindings)[:-MAX_BINDINGS]:
            self._bindings.pop(pid, None)
        self._save()
        return True

    def forget(self, pid: int) -> None:
        if self._bindings.pop(pid, None) is not None:
            self._save()

    def refine(self, scan: HolderScan) -> HolderScan:
        """Set what each hook said against what the scan found: the later word names the session.

        The scan names a process by Claude Code's record where it has one that is the process's
        own, and otherwise by the session its command line resumed. A hook is a later account
        than a command line, and than a record written before it was heard, and an earlier one
        than a record written after: the CLI may have moved again while the daemon was down.
        """
        holders = []
        for holder in scan.holders:
            binding = self._bindings.get(holder.pid)
            if (
                binding is None
                or not binding.is_process(holder.pid, holder.identity[1])
                or binding.start.session_id == holder.session_id
                or holder.recorded_at > binding.at
            ):
                holders.append(holder)
            else:
                holders.append(replace(holder, session_id=binding.start.session_id))
        return HolderScan(holders=holders, complete=scan.complete)

    def _save(self) -> None:
        kept = [_encode(item) for item in self._bindings.values() if item.process is not None]
        self._registry.set_kv(KEY, json.dumps(kept, ensure_ascii=False))


def _encode(binding: Binding) -> dict[str, Any]:
    start = binding.start
    return {
        "pid": start.pid,
        "process": binding.process,
        "at": binding.at,
        "session_id": start.session_id,
        "cwd": start.cwd,
        "source": start.source,
        "transcript_path": start.transcript_path,
    }


def _decode(item: Any) -> Binding | None:
    if not isinstance(item, dict):
        return None
    pid, process, session_id = item.get("pid"), item.get("process"), item.get("session_id")
    at = item.get("at")
    if not (_is_pid(pid) and _is_text(process) and _is_text(session_id)):
        return None
    return Binding(
        start=SessionStart(
            session_id=session_id,
            cwd=str(item.get("cwd") or ""),
            pid=pid,
            source=str(item.get("source") or "startup"),
            transcript_path=str(item.get("transcript_path") or ""),
        ),
        process=process,
        at=at if isinstance(at, int) and not isinstance(at, bool) and at >= 0 else 0,
    )


def _is_pid(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_text(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value)
