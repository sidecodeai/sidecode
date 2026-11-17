"""How often boards may be updated: back-off, the hourly cap and the policy's cached refusals.

None of this is an allowance: the deployment's policy and its billing decide what an account may
spend. These only keep one account, one session or one failing device from turning into a storm of
requests: failures back off, every account has a ceiling per rolling hour, and a refusal answers
for its account for a minute without asking again.
"""

from __future__ import annotations

from collections import deque

from .limits import BACKOFF_SECONDS, HOURLY_CALLS_PER_ACCOUNT, VERDICT_CACHE_SECONDS

HOUR_MS = 3600 * 1000


def backoff_until(failures: int, now_ms: int) -> int:
    """When the back-off after ``failures`` failures in a row ends, ms."""
    step = BACKOFF_SECONDS[min(max(failures, 1), len(BACKOFF_SECONDS)) - 1]
    return now_ms + step * 1000


class HourlyCalls:
    """Model calls per account in the last hour."""

    def __init__(self, limit: int = HOURLY_CALLS_PER_ACCOUNT) -> None:
        self.limit = limit
        self._calls: dict[str, deque[int]] = {}

    def retry_at(self, username: str, now_ms: int) -> int | None:
        """When the account may call again, or None while it is under the cap."""
        calls = self._recent(username, now_ms)
        if len(calls) < self.limit:
            return None
        return calls[0] + HOUR_MS

    def record(self, username: str, now_ms: int) -> None:
        self._calls.setdefault(username, deque()).append(now_ms)

    def forget(self, username: str) -> None:
        self._calls.pop(username, None)

    def _recent(self, username: str, now_ms: int) -> deque[int]:
        calls = self._calls.get(username)
        if calls is None:
            return deque()
        while calls and now_ms - calls[0] >= HOUR_MS:
            calls.popleft()
        if not calls:
            del self._calls[username]
        return calls


class Verdicts:
    """The last refusal the policy gave each account, for `VERDICT_CACHE_SECONDS`."""

    def __init__(self) -> None:
        self._refusals: dict[str, tuple[str, int]] = {}

    def get(self, username: str, now_ms: int) -> str | None:
        found = self._refusals.get(username)
        if found is None:
            return None
        reason, at = found
        if now_ms - at >= VERDICT_CACHE_SECONDS * 1000:
            del self._refusals[username]
            return None
        return reason

    def put(self, username: str, reason: str, now_ms: int) -> None:
        self._refusals[username] = (reason, now_ms)

    def clear(self, username: str) -> None:
        self._refusals.pop(username, None)
