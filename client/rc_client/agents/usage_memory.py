"""What the device keeps between two reads of Anthropic's usage endpoint (A33).

Nothing kept here is a credential: a token is hashed to tell one sign-in from another and is never
stored. What stays is what the endpoint said. Its answer is held for a minute, so that two apps
asking together cost one request and an app asking again costs none, and each account's windows
last read are kept, so that a vendor that cannot answer right now does not blank the page. They are
given back only as `earlier_reading` allows.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..models import AgentLimit
from .earlier_reading import standing_in
from .usage_fetch import Reading

# How long an answer is held, and the longest a vendor's `Retry-After` stretches that to.
HOLD = 60.0
MAX_HOLD = 600.0

Fetch = Callable[[], Awaitable[Reading]]


@dataclass(frozen=True, slots=True)
class Good:
    """The windows an account last had read, and the protocol time they were read at."""

    limits: tuple[AgentLimit, ...]
    checked_at: int


class UsageMemory:
    """The held answers, the reads in flight and the last good windows of one device."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: dict[str, tuple[float, Reading]] = {}
        self._reading: dict[str, asyncio.Future[Reading]] = {}
        self._good: dict[str, Good] = {}

    async def read(self, token: str, fetch: Fetch) -> Reading:
        """What `fetch` brings for `token`: held a minute, shared by readers asking together."""
        key = hashlib.sha256(token.encode()).hexdigest()
        held = self._held.get(key)
        if held is not None and held[0] > self._clock():
            return held[1]
        reading = self._reading.get(key)
        if reading is None:
            reading = asyncio.ensure_future(self._fetch(key, fetch))
            self._reading[key] = reading
        # Shielded: a reader that stops waiting must not cancel the read the others are on.
        return await asyncio.shield(reading)

    def remember(self, who: str, limits: list[AgentLimit], checked_at: int) -> None:
        self._good[who] = Good(tuple(limits), checked_at)

    def recall(self, who: str, now: int) -> Good | None:
        """What `who` last had read, if any of it may stand in at `now` (protocol time)."""
        good = self._good.get(who)
        if good is None:
            return None
        windows = standing_in(good.limits, good.checked_at, now)
        return Good(tuple(windows), good.checked_at) if windows else None

    async def _fetch(self, key: str, fetch: Fetch) -> Reading:
        try:
            reading = await fetch()
        finally:
            del self._reading[key]
        now = self._clock()
        self._held = {name: held for name, held in self._held.items() if held[0] > now}
        if seconds := _hold(reading):
            self._held[key] = (now + seconds, reading)
        return reading


def _hold(reading: Reading) -> float:
    """How long a reading stands in for a new read: a body a minute, a vendor's "wait" its wait."""
    if reading.body is not None:
        return HOLD
    if reading.status == 429:
        return min(max(HOLD, reading.wait or 0), MAX_HOLD)
    return 0.0
