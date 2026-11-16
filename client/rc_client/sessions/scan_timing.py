"""How long each part of a terminal scan took, said once per scan.

A scan that took minutes on a busy machine left nothing behind to say which part
of it did. Each scan now ends with one line naming its phases in milliseconds:
at info when the scan was slow enough to hold back what the apps are shown, and
at debug otherwise, so a daemon that scans every ten seconds does not fill its
log with scans that went as they should.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..logging_setup import logger

log = logger("rc_client.mirror")

# A scan longer than this is worth a line at info: the apps' list waited on it.
SLOW_SCAN = 2.0


class ScanTimer:
    """Laps of one scan, by phase, on the monotonic clock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = self._last = clock()
        self._laps: dict[str, int] = {}

    def lap(self, phase: str) -> None:
        """The time since the last lap, as this phase's."""
        now = self._clock()
        self._laps[phase] = self._laps.get(phase, 0) + int((now - self._last) * 1000)
        self._last = now

    @property
    def total(self) -> float:
        return self._last - self._started

    def report(self, **context: Any) -> None:
        """One line for the whole scan; `context` adds counts, never content."""
        fields = {f"{phase}_ms": ms for phase, ms in self._laps.items()}
        fields["total_ms"] = int(self.total * 1000)
        if self.total >= SLOW_SCAN:
            log.info("terminal scan was slow", **fields, **context)
        else:
            log.debug("terminal scan", **fields, **context)
