"""Mirror Codex threads started in a terminal by tailing their rollout files.

A rollout's first record is `session_meta`, which carries the thread id and the
working directory. Live rows are appended, so growth is tracked by `st_size`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from ...tailing import FileTail
from ..base import Emit
from .heads import heads
from .runtime import SESSIONS_DIR
from .translate import CodexTranslator, _plan_items

MAX_ROLLOUTS = 50
MAX_AGE_DAYS = 14
# The program whose processes write rollouts: a TUI with its own app-server, or
# the shared daemon. Who writes a rollout is what makes its thread a terminal's.
WRITERS = frozenset({"codex"})


@dataclass(slots=True)
class RolloutInfo:
    thread_id: str
    path: str
    cwd: str
    size: int
    mtime: float


def discover(limit: int = MAX_ROLLOUTS, max_age_days: int = MAX_AGE_DAYS) -> list[RolloutInfo]:
    """The most recent rollouts this device may mirror.

    `~/.codex/sessions` holds every Codex thread on the machine, the desktop
    app's and an IDE extension's beside a terminal's, so the `session_meta` a
    rollout opens with decides whose it is before anything else looks at it
    (A18). A foreign rollout is never tailed and its holder is never asked
    about, which is what keeps an application sitting on its own file from
    reading as a terminal. Each file's first record is read once (`heads`).
    """
    root = SESSIONS_DIR
    if not root.is_dir():
        return []
    cutoff = time.time() - max_age_days * 86400
    candidates: list[tuple[float, str, int, int]] = []
    for entry in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff or stat.st_size == 0:
            continue
        candidates.append((stat.st_mtime, str(entry), stat.st_ino, stat.st_size))
    candidates.sort(reverse=True)
    heads.keep({path for _, path, _, _ in candidates})
    found: list[RolloutInfo] = []
    # `limit` counts rollouts this device may mirror, not files walked past:
    # a machine where another application writes most of the history would
    # otherwise fill the whole window with threads nobody here can open.
    for mtime, path, inode, size in candidates:
        if len(found) >= limit:
            break
        head = heads.read(path, inode)
        if head is None or not head.owned or not head.thread_id:
            continue
        found.append(
            RolloutInfo(thread_id=head.thread_id, path=path, cwd=head.cwd, size=size, mtime=mtime)
        )
    return found


def rollout_paths(thread_ids: set[str]) -> dict[str, list[str]]:
    """The rollouts named after each of these threads, from one walk of the history.

    A rollout is named after its thread, `rollout-<time>-<id>.jsonl`, so the
    walk reads no file.
    """
    root = SESSIONS_DIR
    if not thread_ids or not root.is_dir():
        return {}
    lengths = {len(thread_id) for thread_id in thread_ids}
    found: dict[str, list[str]] = {}
    for entry in root.glob("*/*/*/rollout-*.jsonl"):
        thread_id = _named_thread(entry.stem, thread_ids, lengths)
        if thread_id is not None:
            found.setdefault(thread_id, []).append(str(entry))
    return found


def owned_at(paths: dict[str, list[str]]) -> dict[str, bool]:
    """Whether each thread is the device's to publish, as its own rollout says (A18, A59).

    The answer is the `session_meta` the rollout opens with. A thread whose
    rollouts cannot be read, or do not open with its own record, is left out:
    nothing has been said about it.
    """
    owned: dict[str, bool] = {}
    for thread_id, candidates in paths.items():
        for path in candidates:
            try:
                inode = os.stat(path).st_ino
            except OSError:
                continue
            head = heads.read(path, inode)
            if head is not None and head.thread_id == thread_id:
                owned[thread_id] = head.owned
                break
    return owned


def _named_thread(stem: str, thread_ids: set[str], lengths: set[int]) -> str | None:
    """The thread among these whose id ends the file's name, `rollout-<time>-<id>`."""
    for length in lengths:
        candidate = stem[-length:]
        if stem[-length - 1 : -length] == "-" and candidate in thread_ids:
            return candidate
    return None


@dataclass(slots=True)
class RolloutTailer:
    path: str
    cwd: str
    running: bool = False
    translator: CodexTranslator = field(default_factory=CodexTranslator)
    tail: FileTail = field(init=False)

    def __post_init__(self) -> None:
        self.tail = FileTail(path=self.path)

    @property
    def offset(self) -> int:
        return self.tail.offset

    @offset.setter
    def offset(self, value: int) -> None:
        self.tail.offset = value

    def seek_to_end(self) -> None:
        self.tail.seek_to_end()

    @property
    def busy(self) -> bool:
        """Whether a turn is in progress, as the rows read so far leave it."""
        return self.running

    def read_new(self) -> list[dict[str, Any]]:
        return self.tail.read_new()

    def translate(self, row: dict[str, Any]) -> list[Emit]:
        if row.get("type") != "event_msg":
            return []
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return []
        kind = payload.get("type")
        if kind == "task_started":
            self.running = True
            return []
        if kind in {"task_complete", "turn_aborted", "task_failed"}:
            self.running = False
            return []
        if kind == "item_completed":
            item = payload.get("item")
            return self.translator.item(item, True) if isinstance(item, dict) else []
        if kind == "item_started":
            item = payload.get("item")
            return self.translator.item(item, False) if isinstance(item, dict) else []
        if kind == "plan_update":
            return [Emit("todos", {"items": _plan_items(payload.get("plan"))})]
        return []
