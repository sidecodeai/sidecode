"""What the gateway link last said about itself, for `rc-client status`.

The daemon and the CLI are different processes, so the one fact the CLI cannot
work out for itself — that the gateway rejected this device's credential and
the link has stopped trying — is left in a small file beside the rest of the
device state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import state_dir, write_atomic
from .models import now_ms

LINK_FILE = "link.json"
# `rejected` is the only one that needs an operator: the link has given up and
# nothing but a re-enrolment will bring it back.
CONNECTED = "connected"
RETRYING = "retrying"
REJECTED = "rejected"


def path() -> Path:
    return state_dir() / LINK_FILE


@dataclass(slots=True)
class LinkState:
    status: str
    detail: str
    ts: int

    def summary(self) -> str:
        if self.status == REJECTED:
            return f"rejected by the gateway ({self.detail}); run `rc-client enroll` again"
        if self.status == RETRYING:
            return f"reconnecting ({self.detail})" if self.detail else "reconnecting"
        return "connected"


def record(status: str, detail: str = "") -> None:
    """Leave the link's state where the CLI can read it. Never fatal."""
    payload = {"status": status, "detail": detail[:200], "ts": now_ms()}
    try:
        write_atomic(path(), json.dumps(payload).encode("utf-8"))
    except OSError:
        return


def read() -> LinkState | None:
    try:
        raw: Any = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("status"):
        return None
    return LinkState(
        status=str(raw.get("status") or ""),
        detail=str(raw.get("detail") or ""),
        ts=int(raw.get("ts") or 0),
    )
