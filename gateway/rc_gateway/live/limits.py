"""How much a browser may ask of one call: how fast it may send commands, and how many at once."""

from __future__ import annotations

import time
from collections.abc import Callable

from .errors import LiveError

#: Text frames (commands) a browser may send per second, and the burst it may spend at once. The
#: panel sends a handful in a whole call.
COMMANDS_PER_SECOND = 20.0
COMMAND_BURST = 20
#: Commands (Send, Cancel) that may be running at once. A browser that does not read what the
#: gateway answers leaves them waiting, so this is also what bounds the gateway's memory for it.
MAX_COMMANDS_IN_FLIGHT = 4


class CommandGate:
    """A token bucket on the browser's text frames: past its rate the call is closed."""

    def __init__(
        self,
        *,
        rate: float = COMMANDS_PER_SECOND,
        burst: int = COMMAND_BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()

    def check(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens < 1.0:
            raise LiveError("protocol_error", "commands arrive faster than a person sends them")
        self._tokens -= 1.0
