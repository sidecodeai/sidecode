"""The boards the gateway keeps current, in memory (A62).

A board is followed for `FOLLOW_DAYS` after an app last opened it while the account's task board
was on. The hub's hooks run on a device's read loop and must not wait on the disk, so they ask this
set alone: a session that is not here is never read. It is loaded from the store at start, so a
restart follows the same boards, and kept current by every view, every deletion and the sweep.
A board that lapses leaves the set and stays readable on the disk until the sweep deletes it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .limits import DAY_SECONDS, FOLLOW_DAYS

FOLLOW_MS = FOLLOW_DAYS * DAY_SECONDS * 1000


@dataclass(frozen=True)
class Follow:
    session_id: str
    device_id: str
    username: str
    #: When an app last opened the board, ms.
    viewed_at: int

    def lapsed(self, now_ms: int) -> bool:
        return now_ms - self.viewed_at > FOLLOW_MS


class FollowedSet:
    def __init__(self) -> None:
        self._sessions: dict[str, Follow] = {}
        self._devices: dict[str, set[str]] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    def load(self, follows: Iterable[Follow], now_ms: int) -> None:
        for follow in follows:
            if not follow.lapsed(now_ms):
                self.view(follow)

    def view(self, follow: Follow) -> None:
        """Follow a board from an app's view of it, or keep following it from a newer one."""
        known = self._sessions.get(follow.session_id)
        if known is not None and known.viewed_at > follow.viewed_at:
            return
        self.drop(follow.session_id)
        self._sessions[follow.session_id] = follow
        self._devices.setdefault(follow.device_id, set()).add(follow.session_id)

    def get(self, session_id: str, now_ms: int) -> Follow | None:
        """The board's follow while it lasts; a lapsed one leaves the set here."""
        follow = self._sessions.get(session_id)
        if follow is None:
            return None
        if follow.lapsed(now_ms):
            self.drop(session_id)
            return None
        return follow

    def of_device(self, device_id: str, now_ms: int) -> list[Follow]:
        found = [
            self.get(session_id, now_ms) for session_id in list(self._devices.get(device_id, ()))
        ]
        return [follow for follow in found if follow is not None]

    def of_user(self, username: str) -> list[Follow]:
        return [follow for follow in self._sessions.values() if follow.username == username]

    def drop(self, session_id: str) -> None:
        follow = self._sessions.pop(session_id, None)
        if follow is None:
            return
        sessions = self._devices.get(follow.device_id)
        if sessions is not None:
            sessions.discard(session_id)
            if not sessions:
                del self._devices[follow.device_id]

    def drop_device(self, device_id: str) -> None:
        for session_id in list(self._devices.get(device_id, ())):
            self.drop(session_id)

    def drop_user(self, username: str) -> list[str]:
        dropped = [follow.session_id for follow in self.of_user(username)]
        for session_id in dropped:
            self.drop(session_id)
        return dropped

    def prune(self, now_ms: int) -> None:
        for session_id in list(self._sessions):
            self.get(session_id, now_ms)
