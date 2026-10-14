"""Bounded reporting of refused WebSocket upgrades.

A daemon left behind by a wiped machine retries forever: one production gateway saw 685 refused
device upgrades in three hours, all from one address, and every one of them was silent. Logging
each would hand any client the ability to fill the disk, so an address gets one line per window
carrying however many attempts it made in between, and an address that is clearly hammering gets
the cheap rejection instead of a completed handshake.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

WINDOW_SECONDS = 60.0
#: Attempts from one address in one window past which the handshake is no longer completed. Well
#: above what a device honouring its 4401 backoff can reach, so a real revocation still sees the
#: close code that tells it to stop (A4).
FLOOD_ATTEMPTS = 20
MAX_ADDRESSES = 1024


@dataclass(frozen=True)
class Rejection:
    """What to do about one refused upgrade."""

    #: Attempts to report in this line, or ``None`` to stay silent because one was reported
    #: for this address recently.
    report: int | None
    #: The address is past ``FLOOD_ATTEMPTS`` in this window: refuse the handshake outright
    #: rather than accepting it only to close it.
    flooding: bool


@dataclass
class _Bucket:
    reported_at: float
    #: Attempts since the line that opened this window, which is not itself counted again.
    suppressed: int = 0


class RejectionLog:
    def __init__(
        self,
        *,
        window: float = WINDOW_SECONDS,
        flood_attempts: int = FLOOD_ATTEMPTS,
        max_addresses: int = MAX_ADDRESSES,
    ) -> None:
        self.window = window
        self.flood_attempts = flood_attempts
        self.max_addresses = max_addresses
        self._buckets: dict[str, _Bucket] = {}

    def record(self, address: str, *, now: float | None = None) -> Rejection:
        """Count one refused upgrade and say whether to log it and how hard to refuse it."""
        current = time.monotonic() if now is None else now
        self._evict(current)
        bucket = self._buckets.pop(address, None)
        if bucket is not None and current - bucket.reported_at < self.window:
            bucket.suppressed += 1
            # Re-inserted so the map stays ordered oldest first for the cap below.
            self._buckets[address] = bucket
            return Rejection(report=None, flooding=bucket.suppressed + 1 > self.flood_attempts)
        # A new window. The line reports what the last one swallowed plus this attempt, so a
        # steady offender produces one line a window that says how steady it is.
        carried = bucket.suppressed if bucket is not None else 0
        self._buckets[address] = _Bucket(reported_at=current)
        return Rejection(report=carried + 1, flooding=False)

    def _evict(self, now: float) -> None:
        """Bound the map. Scanning only at the cap keeps the ordinary call O(1)."""
        if len(self._buckets) < self.max_addresses:
            return
        stale = now - self.window * 2
        for address in [
            address for address, bucket in self._buckets.items() if bucket.reported_at < stale
        ]:
            del self._buckets[address]
        while len(self._buckets) >= self.max_addresses:
            del self._buckets[next(iter(self._buckets))]
