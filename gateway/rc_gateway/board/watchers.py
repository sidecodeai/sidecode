"""Which app sockets watch which boards, and the `board.updated` frames they are sent (A62).

A socket watches a board from its first `board.get` or `board.refresh` for the session until it
closes, and is sent the board's changes only while it is subscribed to that session, or while its
subscribe is still being answered. A socket is told each state once: a frame goes out only when the
board's revision or status differs from what that socket last received, by reply or by frame.
Every frame leaves through the hub, which holds frames back from an account that may not use the
gateway.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..connections import AppConnection
from ..frames import BOARD_UPDATED
from .wire import Status

#: Boards one socket watches at once; opening another forgets the one opened longest ago.
MAX_WATCHED_PER_SOCKET = 32

Frame = dict[str, Any]
Send = Callable[[AppConnection, Frame], Awaitable[None]]


@dataclass(frozen=True)
class _Told:
    revision: int
    status: Status


def _revision(board: Frame | None) -> int:
    value = board.get("revision") if board is not None else 0
    return value if isinstance(value, int) else 0


def _following(connection: AppConnection, session_id: str) -> bool:
    return connection.subscribed(session_id) or session_id in connection.subscribing


class Watchers:
    def __init__(self, send: Send) -> None:
        self._send = send
        #: Session → its watching sockets, by connection id.
        self._sockets: dict[str, dict[str, AppConnection]] = {}
        #: Connection id → the sessions it watches, the one opened longest ago first.
        self._watching: dict[str, dict[str, None]] = {}
        self._told: dict[tuple[str, str], _Told] = {}
        #: Session → its device and the board the watchers were last given.
        self._boards: dict[str, tuple[str, Frame | None]] = {}

    def watch(
        self,
        connection: AppConnection,
        session_id: str,
        device_id: str,
        board: Frame | None,
        status: Status,
    ) -> None:
        """A socket opened a board and was answered with ``board`` and ``status``."""
        self._sockets.setdefault(session_id, {})[connection.id] = connection
        watching = self._watching.setdefault(connection.id, {})
        watching.pop(session_id, None)
        watching[session_id] = None
        while len(watching) > MAX_WATCHED_PER_SOCKET:
            self._unwatch(connection.id, next(iter(watching)))
        self._boards[session_id] = (device_id, board)
        self._told[(session_id, connection.id)] = _Told(_revision(board), status)

    def watched(self, session_id: str) -> bool:
        return bool(self._sockets.get(session_id))

    def watched_sessions(self) -> list[str]:
        return list(self._sockets)

    def sessions_of(self, username: str) -> list[str]:
        return [
            session_id
            for session_id, sockets in self._sockets.items()
            if any(connection.username == username for connection in sockets.values())
        ]

    def forget_socket(self, connection: AppConnection) -> None:
        for session_id in list(self._watching.get(connection.id, ())):
            self._unwatch(connection.id, session_id)

    def forget_session(self, session_id: str) -> None:
        for connection_id in list(self._sockets.get(session_id, ())):
            self._unwatch(connection_id, session_id)

    def _unwatch(self, connection_id: str, session_id: str) -> None:
        sockets = self._sockets.get(session_id)
        if sockets is not None:
            sockets.pop(connection_id, None)
            if not sockets:
                del self._sockets[session_id]
                self._boards.pop(session_id, None)
        watching = self._watching.get(connection_id)
        if watching is not None:
            watching.pop(session_id, None)
            if not watching:
                del self._watching[connection_id]
        self._told.pop((session_id, connection_id), None)

    async def publish_status(self, session_id: str, status: Status) -> None:
        """The board's status changed; its content did not."""
        known = self._boards.get(session_id)
        if known is not None:
            await self._publish(session_id, known[0], known[1], status)

    async def publish_board(self, session_id: str, board: Frame | None, status: Status) -> None:
        """The board itself changed, or went: ``board`` None after a deletion."""
        known = self._boards.get(session_id)
        if known is not None:
            self._boards[session_id] = (known[0], board)
            await self._publish(session_id, known[0], board, status)

    async def _publish(
        self, session_id: str, device_id: str, board: Frame | None, status: Status
    ) -> None:
        told = _Told(_revision(board), status)
        frame: Frame = {
            "type": BOARD_UPDATED,
            "session_id": session_id,
            "device_id": device_id,
            "board": board,
            "status": status.view(),
        }
        for connection in list(self._sockets.get(session_id, {}).values()):
            key = (session_id, connection.id)
            if connection.closed or not _following(connection, session_id):
                continue
            if self._told.get(key) == told:
                continue
            self._told[key] = told
            await self._send(connection, frame)
