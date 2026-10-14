"""Durable record of issued login sessions.

A signed token is only half the credential: the gateway must be able to revoke one before it
expires. Keeping that record only in memory meant every restart invalidated every token, so a
container that updates itself signed out every browser and every phone even though the token they
held was still valid by its own claims. The rows here survive a restart; the in-memory registry
stays in front as a cache and as the home of the live-socket revocation events.

Rows are small and self-expiring: `prune` drops anything past its expiry or already revoked.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .migrations import Migration, apply_migrations

#: No column has been added since this table's first release; see rc_gateway/migrations.py.
MIGRATIONS: tuple[Migration, ...] = ()


@dataclass(frozen=True)
class StoredSession:
    jti: str
    username: str
    expires_at: int


class AuthSessionStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS login_sessions (
                    jti TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    revoked_at INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS login_sessions_expiry_idx ON login_sessions(expires_at)"
            )
            apply_migrations(connection, MIGRATIONS)

    async def add(self, session: StoredSession, *, now: int | None = None) -> None:
        await asyncio.to_thread(self._add, session, _now(now))

    def _add(self, session: StoredSession, now: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO login_sessions(jti, username, expires_at, created_at, revoked_at) "
                "VALUES (?, ?, ?, ?, NULL) ON CONFLICT(jti) DO NOTHING",
                (session.jti, session.username, session.expires_at, now),
            )

    async def get(self, jti: str, *, now: int | None = None) -> StoredSession | None:
        """Return a live session. Expired and revoked rows read as absent."""
        return await asyncio.to_thread(self._get, jti, _now(now))

    def _get(self, jti: str, now: int) -> StoredSession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT jti, username, expires_at FROM login_sessions "
                "WHERE jti=? AND revoked_at IS NULL AND expires_at>?",
                (jti, now),
            ).fetchone()
        if row is None:
            return None
        return StoredSession(
            jti=str(row["jti"]),
            username=str(row["username"]),
            expires_at=int(row["expires_at"]),
        )

    async def revoke(self, jti: str, *, now: int | None = None) -> bool:
        return await asyncio.to_thread(self._revoke, jti, _now(now))

    def _revoke(self, jti: str, now: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE login_sessions SET revoked_at=? WHERE jti=? AND revoked_at IS NULL",
                (now, jti),
            )
        return cursor.rowcount == 1

    async def revoke_for_user(self, username: str, *, now: int | None = None) -> list[str]:
        """Revoke every live session of one account. Returns the ids that were live (A24)."""
        return await asyncio.to_thread(self._revoke_for_user, username, _now(now))

    def _revoke_for_user(self, username: str, now: int) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT jti FROM login_sessions WHERE username=? AND revoked_at IS NULL",
                (username,),
            ).fetchall()
            connection.execute(
                "UPDATE login_sessions SET revoked_at=? WHERE username=? AND revoked_at IS NULL",
                (now, username),
            )
        return [str(row["jti"]) for row in rows]

    async def prune(self, *, now: int | None = None) -> int:
        """Drop expired and revoked rows. Returns how many were removed."""
        return await asyncio.to_thread(self._prune, _now(now))

    def _prune(self, now: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM login_sessions WHERE expires_at<=? OR revoked_at IS NOT NULL", (now,)
            )
        return int(cursor.rowcount)

    async def count(self) -> int:
        return await asyncio.to_thread(self._count)

    def _count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM login_sessions").fetchone()[0])


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else value
