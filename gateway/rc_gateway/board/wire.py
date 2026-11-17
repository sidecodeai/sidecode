"""The board's objects as the apps receive them: `Board` and `BoardStatus` (A62)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import BoardRow

OFF = "off"
UNAVAILABLE = "unavailable"
CURRENT = "current"
UPDATING = "updating"
STALE = "stale"

DEVICE_OFFLINE = "device_offline"
HISTORY_UNAVAILABLE = "history_unavailable"
MODEL_FAILED = "model_failed"
INVALID_OUTPUT = "invalid_output"
RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class Status:
    state: str
    reason: str | None = None
    #: For `stale`: when a back-off or a cap ends, ms.
    retry_at: int | None = None

    def view(self) -> dict[str, Any]:
        """``objects.json#/$defs/BoardStatus``."""
        view: dict[str, Any] = {"state": self.state, "reason": self.reason}
        if self.state == STALE:
            view["retry_at"] = self.retry_at
        return view


STATUS_OFF = Status(OFF)
STATUS_CURRENT = Status(CURRENT)
STATUS_UPDATING = Status(UPDATING)


def unavailable(reason: str) -> Status:
    return Status(UNAVAILABLE, reason)


def stale(reason: str, retry_at: int | None = None) -> Status:
    return Status(STALE, reason, retry_at)


def board_view(row: BoardRow | None) -> dict[str, Any] | None:
    """``objects.json#/$defs/Board``, or None for a board with no successful update yet."""
    if row is None or row.revision < 1:
        return None
    return {
        "session_id": row.session_id,
        "device_id": row.device_id,
        "revision": row.revision,
        "items": [item.view() for item in row.items],
        "updated_at": row.updated_at,
        "partial": row.partial,
    }
