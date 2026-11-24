"""The boards, one row per session, in their own SQLite file under ``DATA_DIR`` (A62).

The file follows the other stores: opened at 0600, WAL, and additive migrations for anything a
later change adds. A row holds the board and the cursors of the last update, never anything the
board was made from: the history read from the device and the text the model received live in
memory for one update and are gone after it.

A row is created, empty, when an app first opens the conversation with the account's task board
on. It leaves with its session, its device, its account, the account's task board turned off, and
after `UNVIEWED_DAYS` nobody opened it, unless an app is watching it.

Every write of a board takes the next value of one counter for the whole store, kept in the same
file and advanced in the same transaction, so a session's revision only grows, even when its board
was deleted and made again: an app that holds a board never mistakes a newer one for an older one.
Writes of the cursors, the back-off or the view are not writes of the board and keep its revision.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..migrations import Migration, apply_migrations
from .merge import Item, item_from_stored

#: The store's file under ``DATA_DIR``.
STORE_FILE = "boards.sqlite3"

#: Columns added since this table's first release; see rc_gateway/migrations.py.
MIGRATIONS: tuple[Migration, ...] = ()

_COLUMNS = (
    "session_id, device_id, username, revision, items, next_item, through_seq, checked_seq,"
    " queue_hash, partial, updated_at, viewed_at, failures, retry_at"
)


@dataclass(frozen=True)
class BoardRow:
    session_id: str
    device_id: str
    username: str
    #: 0 until the first successful update; then the store's counter at the board's latest write.
    revision: int = 0
    items: tuple[Item, ...] = ()
    #: The number of the next new item's id, never given twice.
    next_item: int = 1
    #: The highest `seq` the board has read.
    through_seq: int = 0
    #: The session's indexed `last_seq` when the last update fetched, and its queue's hash.
    checked_seq: int = 0
    queue_hash: str = ""
    partial: bool = False
    #: The last successful update, ms; 0 before the first.
    updated_at: int = 0
    #: The last time an app opened the board with the task board on, ms.
    viewed_at: int = 0
    #: Failures in a row, and when the back-off they earned ends (ms).
    failures: int = 0
    retry_at: int | None = None

    def cleared(self) -> BoardRow:
        """The row a rebuild starts from: no items, nothing read, the id counter kept."""
        return replace(self, items=(), through_seq=0, checked_seq=0, queue_hash="", partial=False)


class BoardStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # A board quotes what a person asked for, so the file is no more readable than the others.
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS board_meta (
                    id           INTEGER PRIMARY KEY CHECK (id = 1),
                    revision     INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS boards (
                    session_id   TEXT PRIMARY KEY,
                    device_id    TEXT NOT NULL,
                    username     TEXT NOT NULL,
                    revision     INTEGER NOT NULL DEFAULT 0,
                    items        TEXT NOT NULL DEFAULT '[]',
                    next_item    INTEGER NOT NULL DEFAULT 1,
                    through_seq  INTEGER NOT NULL DEFAULT 0,
                    checked_seq  INTEGER NOT NULL DEFAULT 0,
                    queue_hash   TEXT NOT NULL DEFAULT '',
                    partial      INTEGER NOT NULL DEFAULT 0,
                    updated_at   INTEGER NOT NULL DEFAULT 0,
                    viewed_at    INTEGER NOT NULL,
                    failures     INTEGER NOT NULL DEFAULT 0,
                    retry_at     INTEGER
                );
                CREATE INDEX IF NOT EXISTS boards_user_idx ON boards(username);
                CREATE INDEX IF NOT EXISTS boards_device_idx ON boards(device_id);
                CREATE INDEX IF NOT EXISTS boards_viewed_idx ON boards(viewed_at);
                INSERT OR IGNORE INTO board_meta(id, revision)
                    SELECT 1, COALESCE(MAX(revision), 0) FROM boards;
                """
            )
            apply_migrations(connection, MIGRATIONS)

    async def get(self, session_id: str) -> BoardRow | None:
        return await asyncio.to_thread(self._get, session_id)

    def _get(self, session_id: str) -> BoardRow | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE session_id=?", (session_id,)
            ).fetchone()
        return None if row is None else _from_row(row)

    async def create(self, session_id: str, device_id: str, username: str, viewed_at: int) -> None:
        """The empty row of a board opened for the first time; an existing row is left alone."""
        await asyncio.to_thread(self._create, session_id, device_id, username, viewed_at)

    def _create(self, session_id: str, device_id: str, username: str, viewed_at: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO boards(session_id, device_id, username, viewed_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, device_id, username, viewed_at),
            )

    async def viewed(self, session_id: str, viewed_at: int) -> None:
        await asyncio.to_thread(self._viewed, session_id, viewed_at)

    def _viewed(self, session_id: str, viewed_at: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE boards SET viewed_at=MAX(viewed_at, ?) WHERE session_id=?",
                (viewed_at, session_id),
            )

    async def write_board(self, row: BoardRow) -> BoardRow | None:
        """Write the board and its cursors under the store's next revision, which it returns with.

        ``None`` when the row is gone or changed hands meanwhile: nothing is written then, and the
        counter does not move.
        """
        return await asyncio.to_thread(self._write_board, row)

    def _write_board(self, row: BoardRow) -> BoardRow | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE board_meta SET revision = revision + 1 WHERE id = 1")
            (revision,) = connection.execute(
                "SELECT revision FROM board_meta WHERE id = 1"
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE boards SET revision=?, items=?, next_item=?, through_seq=?, checked_seq=?,
                    queue_hash=?, partial=?, updated_at=?, failures=?, retry_at=?
                WHERE session_id=? AND username=?
                """,
                (
                    revision,
                    json.dumps([item.stored() for item in row.items], ensure_ascii=False),
                    row.next_item,
                    row.through_seq,
                    row.checked_seq,
                    row.queue_hash,
                    int(row.partial),
                    row.updated_at,
                    row.failures,
                    row.retry_at,
                    row.session_id,
                    row.username,
                ),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                return None
            connection.commit()
        finally:
            connection.close()
        return replace(row, revision=int(revision))

    async def write_state(self, row: BoardRow) -> bool:
        """Write the cursors and the back-off, not the board; False when the row is gone."""
        return await asyncio.to_thread(self._write_state, row)

    def _write_state(self, row: BoardRow) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE boards SET through_seq=?, checked_seq=?, queue_hash=?, failures=?, retry_at=?
                WHERE session_id=? AND username=?
                """,
                (
                    row.through_seq,
                    row.checked_seq,
                    row.queue_hash,
                    row.failures,
                    row.retry_at,
                    row.session_id,
                    row.username,
                ),
            )
            return cursor.rowcount > 0

    async def followed(self, since: int) -> list[BoardRow]:
        """The rows opened at or after ``since`` (ms): what a restart follows again."""
        return await asyncio.to_thread(self._followed, since)

    def _followed(self, since: int) -> list[BoardRow]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM boards WHERE viewed_at>=?", (since,)
            ).fetchall()
        return [_from_row(row) for row in rows]

    async def remove(self, session_id: str) -> None:
        await self._delete("session_id", session_id)

    async def remove_for_device(self, device_id: str) -> None:
        await self._delete("device_id", device_id)

    async def remove_for_user(self, username: str) -> None:
        await self._delete("username", username)

    async def _delete(self, column: str, value: str) -> None:
        await asyncio.to_thread(self._delete_rows, column, value)

    def _delete_rows(self, column: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(f"DELETE FROM boards WHERE {column}=?", (value,))

    async def clear(self) -> int:
        """Delete every board, and say how many there were. The revision counter stays, so a
        board made afterwards still reads as newer than any an app holds."""
        return await asyncio.to_thread(self._clear)

    def _clear(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("DELETE FROM boards").rowcount)

    async def sweep(self, before: int, *, keep: Collection[str] = ()) -> list[str]:
        """Delete the boards nobody opened since ``before`` (ms) but those in ``keep``.

        Returns the sessions whose boards went.
        """
        return await asyncio.to_thread(self._sweep, before, frozenset(keep))

    def _sweep(self, before: int, keep: frozenset[str]) -> list[str]:
        with self._connect() as connection:
            stale = [
                str(row[0])
                for row in connection.execute(
                    "SELECT session_id FROM boards WHERE viewed_at<?", (before,)
                ).fetchall()
            ]
            gone = [session_id for session_id in stale if session_id not in keep]
            connection.executemany(
                "DELETE FROM boards WHERE session_id=? AND viewed_at<?",
                [(session_id, before) for session_id in gone],
            )
        return gone


def _from_row(row: sqlite3.Row) -> BoardRow:
    return BoardRow(
        session_id=str(row["session_id"]),
        device_id=str(row["device_id"]),
        username=str(row["username"]),
        revision=int(row["revision"]),
        items=_items(row["items"]),
        next_item=int(row["next_item"]),
        through_seq=int(row["through_seq"]),
        checked_seq=int(row["checked_seq"]),
        queue_hash=str(row["queue_hash"]),
        partial=bool(row["partial"]),
        updated_at=int(row["updated_at"]),
        viewed_at=int(row["viewed_at"]),
        failures=int(row["failures"]),
        retry_at=None if row["retry_at"] is None else int(row["retry_at"]),
    )


def _items(raw: Any) -> tuple[Item, ...]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    found = (item_from_stored(entry) for entry in data if isinstance(entry, dict))
    return tuple(item for item in found if item is not None)
