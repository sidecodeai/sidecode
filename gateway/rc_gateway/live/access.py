"""What the assistant may see and do, always as one account.

Every read and write starts from the account's own devices. A session id the model supplies is
resolved against that set only; one that is not in it is answered exactly like one that does not
exist.
"""

from __future__ import annotations

from typing import Any

from ..state import GatewayState
from .errors import LiveError

Session = dict[str, Any]
Event = dict[str, Any]

MAX_TAIL_EVENTS = 400


class OwnedSession(Session):
    """A session summary the account owns. Only ``AccountAccess`` makes one, so anything that asks
    for a session's events with one has been through the ownership check."""


class AccountAccess:
    def __init__(self, state: GatewayState, username: str) -> None:
        self._state = state
        self.username = username

    async def _devices(self) -> dict[str, str]:
        records = await self._state.devices.list_for_user(self.username)
        return {record.device_id: record.name for record in records}

    async def sessions(self) -> list[OwnedSession]:
        names = await self._devices()
        listed = await self._state.index.list_sessions(device_ids=list(names), archived=False)
        return [
            OwnedSession({**item, "_device_name": names.get(str(item.get("device_id")), "")})
            for item in listed
        ]

    async def resolve(self, ref: str) -> OwnedSession:
        """One of the account's sessions, by id or by a title or folder name that is unambiguous."""
        needle = ref.strip()
        if not needle:
            raise LiveError("unknown_session", "no such session")
        sessions = await self.sessions()
        for item in sessions:
            if item.get("session_id") == needle:
                return item
        lowered = needle.lower()
        found = [item for item in sessions if lowered in _labels(item)]
        if len(found) == 1:
            return found[0]
        if not found:
            raise LiveError("unknown_session", "no such session")
        raise LiveError("ambiguous_session", "several sessions match; ask which one")

    def tail(self, session: OwnedSession) -> list[Event]:
        """The newest buffered events of one of the account's own sessions."""
        return self._state.hub.session_tail(str(session.get("session_id")), MAX_TAIL_EVENTS)

    async def send(self, session_id: str, text: str) -> str:
        """Send as the account's own app would (``session.send``, auto). Returns ``accepted``."""
        reply = await self._state.hub.send_session_as(self.username, session_id, text)
        if reply.get("ok") is not True:
            error = reply.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            raise LiveError("send_failed", str(code or "send failed"))
        result = reply.get("result")
        return str(result.get("accepted", "sent")) if isinstance(result, dict) else "sent"


def _labels(item: Session) -> str:
    cwd = str(item.get("cwd") or "").rstrip("/")
    folder = cwd.rsplit("/", 1)[-1]
    return f"{item.get('title') or ''}\n{folder}".lower()
