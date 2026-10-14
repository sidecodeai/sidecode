"""A gateway-wide ceiling on the bytes of large frames in flight.

§5 lets one ``session.send`` carry eight attachments of 6 MiB, which base64 inflates to a 64 MiB
wire frame. Forwarding one holds the parsed payload and the string queued for the device at the
same time, so a few concurrent maximal sends are more memory than the container is given. The
ordinary frame — an event, a reply, a short message — never touches the budget; only a payload
past ``LARGE_FRAME_BYTES`` claims room, and the sender is answered ``too_large`` when there is
none. The claim is given back when the device's queue lets the frame go.

A maximal frame was measured through this path: 64 MiB on the wire, 128 MiB of live allocation at
the moment it is queued (the parsed payload plus the string the device's queue holds) and a 142 MiB
peak. The arithmetic against ``docker-compose.yml``'s ``mem_limit: 512m``:

====================================================================  ========
Interpreter, FastAPI, httpx, the SQLite stores and live connections    ~80 MiB
The one frame the budget admits, live when it is queued                128 MiB
Replay buffers at their stated worst case (`hub.MAX_REPLAY_BUFFERS`)    64 MiB
A second frame read and parsed before the budget refuses it            128 MiB
The ``websockets`` reassembly of that second frame                      64 MiB
====================================================================  ========

which totals 464 MiB. Admitting a second frame would add its 128 MiB and reach 592 MiB, over the
limit, so the budget holds one — and the refusal is a ``too_large`` reply the app can show, not an
OOM kill that takes every device link and every in-flight request with it.
"""

from __future__ import annotations

from .frames import WS_MAX_MESSAGE_BYTES

#: Below this a frame is not worth accounting: replies and events are capped at 64 KiB (§4), so
#: only a message carrying attachments can reach it.
LARGE_FRAME_BYTES = 1024 * 1024
#: One maximal frame. See the arithmetic above.
MAX_INFLIGHT_BYTES = WS_MAX_MESSAGE_BYTES


class ByteBudget:
    """How many bytes of large frames the gateway is prepared to hold at once."""

    def __init__(
        self,
        capacity: int = MAX_INFLIGHT_BYTES,
        *,
        threshold: int = LARGE_FRAME_BYTES,
    ) -> None:
        self.capacity = capacity
        self.threshold = threshold
        self._held = 0

    @property
    def held(self) -> int:
        return self._held

    def claim(self, size: int) -> bool:
        """Reserve room for one frame. A frame under the threshold is never refused."""
        if size < self.threshold:
            return True
        if self._held + size > self.capacity:
            return False
        self._held += size
        return True

    def release(self, size: int) -> None:
        if size < self.threshold:
            return
        self._held = max(0, self._held - size)
