"""A probe that notices when the event loop stops running on time.

A device that stops answering the gateway for half a minute looks exactly like
a device with a bad network, and the two are fixed in completely different
places. This settles the question from the inside: a task that asks only to be
woken in a second, and says so when it is woken much later than that.
"""

from __future__ import annotations

import asyncio
import time

from .logging_setup import logger

log = logger("rc_client.looplag")

TICK = 1.0
# Well past any ordinary scheduling delay, and under the gateway's 60 s silence
# timeout, so a stall that costs the link is always reported before the drop.
LAG_THRESHOLD = 5.0


async def watch_loop_lag(tick: float = TICK, threshold: float = LAG_THRESHOLD) -> None:
    """Sleep in short steps forever, logging every step that overran."""
    while True:
        started = time.monotonic()
        await asyncio.sleep(tick)
        late = time.monotonic() - started - tick
        if late > threshold:
            log.warning("event loop stalled", late_seconds=round(late, 1))
