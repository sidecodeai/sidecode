"""A per-account cap on call time per day, kept in memory (a restart starts the day afresh)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

#: Accounts remembered at once; a day's entries for other accounts are dropped as the day turns.
MAX_ACCOUNTS = 1024


class DailyBudget:
    """Seconds of call time each account may use per UTC day."""

    def __init__(self, seconds: float, wall_clock: Callable[[], float] = time.time) -> None:
        self._seconds = seconds
        self._wall = wall_clock
        self._day = self._today()
        self._used: dict[str, float] = {}

    def _today(self) -> int:
        return datetime.fromtimestamp(self._wall(), UTC).toordinal()

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._used.clear()

    def remaining(self, username: str) -> float:
        self._roll()
        return max(0.0, self._seconds - self._used.get(username, 0.0))

    def charge(self, username: str, seconds: float) -> None:
        self._roll()
        if username not in self._used and len(self._used) >= MAX_ACCOUNTS:
            return
        self._used[username] = self._used.get(username, 0.0) + max(0.0, seconds)
