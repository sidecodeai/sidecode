"""The order a session's held messages go out in (amendment A43).

A queue is kept in `ts` order, which is the order it drains in. An app edits a
queued message by taking it out and sending the new words back with the `ts`
the entry had, so the device holds that message in front of every entry with a
later one: it waits where it was, however the queue moved meanwhile. A message
the device puts back itself, an injection the CLI absorbed or the bridge
refused, goes back under its own `ts` the same way. Anything else joins the end.
"""

from __future__ import annotations

from typing import Any

from ..errors import RcError
from ..models import now_ms


def queue_ts_from(params: dict[str, Any]) -> int | None:
    """The `ts` a `session.send` asks its message to be held under, if any.

    `True` is an `int` to Python and never a timestamp, and a string or a float
    that reads like one is not one either.
    """
    value = params.get("queue_ts")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RcError("bad_request", "queue_ts must be a non-negative integer")
    return value


def hold(queue: list[dict[str, Any]], item: dict[str, Any], queue_ts: int | None) -> None:
    """Put one message into the queue, in its place.

    With `queue_ts` it is held under that `ts`, in front of the first entry with
    a later one and so behind any with the same. Without it, it joins the end,
    stamped with the current time, or one past the last entry's `ts` when that
    is not earlier: two messages that arrive within one millisecond would
    otherwise share a `ts`, and an edited one, which comes back with nothing
    else, could not tell which of them it stood before.
    """
    if queue_ts is None:
        item["ts"] = max(now_ms(), queue[-1]["ts"] + 1) if queue else now_ms()
        queue.append(item)
        return
    item["ts"] = queue_ts
    place = next((index for index, held in enumerate(queue) if held["ts"] > queue_ts), len(queue))
    queue.insert(place, item)


def snapshot_entry(item: dict[str, Any]) -> dict[str, Any]:
    """One held message as `queue.pending[]` carries it (5.12).

    Its files stay on the device and no frame brings them back, so the entry
    says only how many there are, and only when there are any: that is how an
    app knows it can remove the message but not edit it.
    """
    entry: dict[str, Any] = {"id": item["id"], "text": item["text"], "ts": item["ts"]}
    files = len(item.get("attachments") or [])
    if files:
        entry["attachments"] = files
    return entry
