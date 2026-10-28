"""The browser player's own report: its cumulative counters and its playout delay.

``playback {underruns, gap_ms, restarts, delay_ms}``. The counters are kept only for the call's
usage line, beside the pacer's own estimate of the same thing, so a difference between the two
shows whether audio was lost on the way or at the end. The delay is what the player holds each
frame before it plays it: the call adds it to the end of the assistant's voice (``playback.py``),
because the voice is heard that much later than the gateway sent it.
"""

from __future__ import annotations

from typing import Any

#: A count past this is a client's mistake or mischief, not a measurement; it is clamped to it.
MAX_REPORTED = 1_000_000_000
#: The longest playout delay taken at its word; a longer one is clamped to this.
MAX_DELAY_MS = 2000
COUNTS = ("underruns", "gap_ms", "restarts")


class PlaybackReport:
    def __init__(self) -> None:
        self._counts: dict[str, int] | None = None
        self._delay_ms: int | None = None

    def note(self, command: dict[str, Any]) -> None:
        """Keep what a report says that is usable, and ignore the rest of it.

        The three counters count only together; the delay stands on its own.
        """
        counts = _counts(command)
        if counts is not None:
            self._counts = counts
        delay = _count(command.get("delay_ms"))
        if delay is not None:
            self._delay_ms = min(delay, MAX_DELAY_MS)

    @property
    def delay_seconds(self) -> float | None:
        """The playout delay as last reported, or ``None`` before the browser has said."""
        return None if self._delay_ms is None else self._delay_ms / 1000

    def record(self) -> dict[str, int | None]:
        """The latest values, or ``None`` for each the browser never reported."""
        counts = self._counts or {}
        return {
            **{f"browser_{name}": counts.get(name) for name in COUNTS},
            "browser_delay_ms": self._delay_ms,
        }


def _count(value: Any) -> int | None:
    """A non-negative integer, clamped; ``None`` for anything else, booleans and floats included."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return min(value, MAX_REPORTED)
    return None


def _counts(command: dict[str, Any]) -> dict[str, int] | None:
    counts: dict[str, int] = {}
    for name in COUNTS:
        value = _count(command.get(name))
        if value is None:
            return None
        counts[name] = value
    return counts
