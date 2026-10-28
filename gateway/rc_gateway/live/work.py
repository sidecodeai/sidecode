"""What the backend is busy with: the browser's "working" hint, and the usage line's timings.

A backend response is in flight from the first nested event seen for its id until its final event
(``completed``, ``incomplete`` or ``failed``). A tool the gateway runs for it is work until its
answer has been handed back, and the response the gateway then asks for is in flight until its first
event arrives, so the hint does not blink off between a tool's answer and the response that uses it.
Events that do not name their response are not attributed to any, so they count for nothing.
Numbers and response ids only; nothing said or returned by a tool is ever kept.
"""

from __future__ import annotations

import math
from typing import Any

from .stats import Samples
from .usage import MAX_TRACKED_RESPONSES, is_final_response

#: A response with no final event after this long is no longer counted as work; its final event, if
#: it does come, is still timed.
STALE_RESPONSE_SECONDS = 60.0
#: How long the response the gateway asked for may take to show its first event before the hint
#: stops waiting for it.
CONTINUATION_SECONDS = 10.0


class WorkTracker:
    def __init__(self) -> None:
        #: Responses in flight, by id, with the time their first event was seen.
        self._flying: dict[str, float] = {}
        #: Responses that have ended, so a repeated or late event does not start them again.
        self._finished: dict[str, None] = {}
        self._tools = 0
        self._continuation_until = -math.inf
        self.backend = Samples()
        self.tools = Samples()

    def note_event(self, inner: dict[str, Any], now: float) -> None:
        """One of the backend's nested Responses events."""
        identifier = _response_id(inner)
        if identifier is None or identifier in self._finished:
            return
        started = self._flying.get(identifier)
        final = is_final_response(inner)
        if started is None:
            # A response not seen before is the one the gateway asked for, if it was waiting.
            self._continuation_until = -math.inf
            if not final:
                self._flying[identifier] = now
                _trim(self._flying)
                return
        if final:
            self._flying.pop(identifier, None)
            self._finished[identifier] = None
            _trim(self._finished)
            if started is not None:
                self.backend.add(now - started)

    def tool_started(self) -> None:
        """A tool call has been taken up: it is work until its answer has been handed back."""
        self._tools += 1

    def tool_ran(self, seconds: float) -> None:
        self.tools.add(seconds)

    def continuation_asked(self, now: float) -> None:
        """The gateway has asked the backend to go on with a tool's answer."""
        self._continuation_until = now + CONTINUATION_SECONDS

    def tool_ended(self) -> None:
        self._tools = max(0, self._tools - 1)

    def working(self, now: float) -> bool:
        """Whether a response is in flight, a tool is being answered, or a response is awaited."""
        return (
            self._tools > 0
            or now < self._continuation_until
            or any(now - started < STALE_RESPONSE_SECONDS for started in self._flying.values())
        )

    def record(self) -> dict[str, int | None]:
        return {
            "backend_ms_median": self.backend.median_ms(),
            "backend_ms_max": self.backend.max_ms(),
            "tool_ms_max": self.tools.max_ms(),
        }


def _response_id(inner: dict[str, Any]) -> str | None:
    response = inner.get("response")
    identifier = response.get("id") if isinstance(response, dict) else None
    return identifier if isinstance(identifier, str) and identifier else None


def _trim(tracked: dict[str, Any]) -> None:
    """Forget the oldest entries past the bound."""
    while len(tracked) > MAX_TRACKED_RESPONSES:
        del tracked[next(iter(tracked))]
