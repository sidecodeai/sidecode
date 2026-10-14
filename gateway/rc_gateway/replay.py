"""Per-session replay buffer.

The device owns history; the gateway keeps the tail so a reconnecting app can catch up without a
round trip to a possibly sleeping laptop. Bounds are the smaller of 2 000 events or 4 MiB per
session (protocol §6). When the buffer no longer covers the app's cursor the subscribe reply says
``resync: true`` and the app pages history from the device instead.
"""

from __future__ import annotations

from collections import deque
from typing import Any

MAX_EVENTS = 2000
MAX_BYTES = 4 * 1024 * 1024

Event = dict[str, Any]


class ReplayBuffer:
    def __init__(self, *, max_events: int = MAX_EVENTS, max_bytes: int = MAX_BYTES) -> None:
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._events: deque[tuple[int, Event, int]] = deque()
        self._bytes = 0
        self.last_seq = 0

    def __len__(self) -> int:
        return len(self._events)

    @property
    def byte_size(self) -> int:
        return self._bytes

    @property
    def first_seq(self) -> int | None:
        return self._events[0][0] if self._events else None

    def append(self, event: Event, size: int) -> bool:
        """Append one event. Returns False for a late or duplicate ``seq``."""
        seq = event.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return False
        if seq <= self.last_seq:
            return False
        self._events.append((seq, event, size))
        self._bytes += size
        self.last_seq = seq
        while self._events and (
            len(self._events) > self.max_events or self._bytes > self.max_bytes
        ):
            _, _, dropped = self._events.popleft()
            self._bytes -= dropped
        return True

    def replay_from(self, since_seq: int | None) -> tuple[list[Event], bool]:
        """Return ``(events, resync)`` for an app cursor.

        ``since_seq is None`` means a cold subscribe: the app has no cache and pages history
        itself, so nothing is replayed. Otherwise the buffer must still hold ``since_seq + 1`` for
        the tail to be complete; when it does not, the app is told to resynchronise.
        """
        if since_seq is None:
            return [], False
        if since_seq >= self.last_seq:
            return [], False
        first = self.first_seq
        if first is None or first > since_seq + 1:
            return [], True
        return [event for seq, event, _ in self._events if seq > since_seq], False
