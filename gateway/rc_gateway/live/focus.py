"""Which session's page the browser has open: a hint for the assistant, never a permission.

The browser says which chat page is on screen, so that "this session" can mean something. It is
only ever matched against sessions the account already owns and lists, and it grants nothing: a
focus on a session that is not the account's marks no row and is never echoed back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_ID_CHARS = 200


@dataclass(frozen=True)
class Focus:
    device_id: str
    session_id: str

    def matches(self, session: dict[str, Any]) -> bool:
        return (
            session.get("device_id") == self.device_id
            and session.get("session_id") == self.session_id
        )


def parse_focus(command: dict[str, Any]) -> Focus | None:
    """The focus a ``focus`` command names, or ``None`` when it names none or is malformed."""
    device_id, session_id = command.get("device_id"), command.get("session_id")
    if (
        isinstance(device_id, str)
        and isinstance(session_id, str)
        and 0 < len(device_id) <= MAX_ID_CHARS
        and 0 < len(session_id) <= MAX_ID_CHARS
    ):
        return Focus(device_id, session_id)
    return None
