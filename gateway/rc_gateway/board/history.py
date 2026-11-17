"""Reading a board's input from the device: `session.history`, brief, newest pages first (A62).

The gateway asks on its own account, as A56's catch-up does, and only for what the selection needs:
the newest page, then older ones while they could still hold it, at most `HISTORY_PAGES`. A reply
that is not `ok`, that does not come in time or that is larger than `HISTORY_PAGE_BYTES` ends the
read. Nothing of what comes back is logged or kept beyond the update that asked for it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..connections import encode
from .limits import HISTORY_PAGE_BYTES, HISTORY_PAGE_EVENTS, HISTORY_PAGE_SECONDS, HISTORY_PAGES
from .window import Event, enough, seq_of

Frame = dict[str, Any]
#: Ask a connected device for something on the gateway's own account: the reply, or None.
Ask = Callable[[str, Frame, float], Awaitable[Frame | None]]


@dataclass(frozen=True)
class History:
    events: tuple[Event, ...]
    #: Whether the last page fetched said older events exist.
    has_more: bool
    pages: int

    @property
    def highest_seq(self) -> int:
        return max((seq_of(event) for event in self.events), default=0)


async def read_history(
    ask: Ask, device_id: str, session_id: str, through_seq: int
) -> History | None:
    """The newest pages the selection needs, or ``None`` when the device could not give them."""
    events: list[Event] = []
    before: int | None = None
    for page in range(1, HISTORY_PAGES + 1):
        fetched = await _page(ask, device_id, session_id, before)
        if fetched is None:
            return None
        found, has_more = fetched
        events.extend(found)
        if not has_more or not found or enough(events, through_seq=through_seq):
            return History(tuple(events), has_more, page)
        before = min(seq_of(event) for event in events)
    return History(tuple(events), True, HISTORY_PAGES)


async def _page(
    ask: Ask, device_id: str, session_id: str, before: int | None
) -> tuple[list[Event], bool] | None:
    request: Frame = {
        "type": "session.history",
        "session_id": session_id,
        "limit": HISTORY_PAGE_EVENTS,
        "brief": True,
    }
    if before is not None:
        request["before_seq"] = before
    reply = await ask(device_id, request, HISTORY_PAGE_SECONDS)
    if reply is None or reply.get("ok") is not True:
        return None
    if len(encode(reply).encode("utf-8")) > HISTORY_PAGE_BYTES:
        return None
    result = reply.get("result")
    events = result.get("events") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not isinstance(events, list):
        return None
    kept = [event for event in events if isinstance(event, dict) and seq_of(event) >= 0]
    return kept, bool(result.get("has_more"))
