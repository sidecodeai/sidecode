"""The accounts on this gateway, and whether it is taking new ones (A24).

``admin`` is the operator: its row exists from the first start, and its password is ``RC_PASSWORD``
rather than anything stored here, so the hash column stays empty for it. Every other account is
made by registering while the admin allows it, or by the admin, and carries a scrypt hash from
``accounts``.

Registration is a one-row setting in the same file, closed on a fresh gateway, so the operator
decides when the door is open.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounts import (
    ADMIN_USERNAME,
    ROLE_ADMIN,
    ROLE_MEMBER,
    STATE_ACTIVE,
    hash_password,
    normalize_username,
)
from .migrations import Migration, apply_migrations

#: No column has been added since this table's first release; see rc_gateway/migrations.py.
MIGRATIONS: tuple[Migration, ...] = ()

REGISTRATION_KEY = "registration_open"


@dataclass(frozen=True)
class UserRecord:
    username: str
    role: str
    state: str
    created_at: int
    last_login_at: int | None
    #: ``None`` for ``admin``, whose password is the configured one and is never stored.
    password_hash: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE


class UserStore:
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
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    state TEXT NOT NULL,
                    password_hash TEXT,
                    created_at INTEGER NOT NULL,
                    last_login_at INTEGER
                )
                """
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            apply_migrations(connection, MIGRATIONS)
            connection.execute(
                "INSERT INTO users(username, role, state, password_hash, created_at, last_login_at)"
                " VALUES (?, ?, ?, NULL, ?, NULL) ON CONFLICT(username) DO NOTHING",
                (ADMIN_USERNAME, ROLE_ADMIN, STATE_ACTIVE, int(time.time())),
            )
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, '0') ON CONFLICT(key) DO NOTHING",
                (REGISTRATION_KEY,),
            )

    # ---- reads ----

    async def get(self, username: str) -> UserRecord | None:
        return await asyncio.to_thread(self._get, normalize_username(username))

    def _get(self, username: str) -> UserRecord | None:
        if not username:
            return None
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM users WHERE username=?", (username,)
            ).fetchone()
        return _record(row) if row is not None else None

    async def list_accounts(self) -> list[UserRecord]:
        """Every account, oldest first, as the admin lists them."""
        return await asyncio.to_thread(self._list_accounts)

    def _list_accounts(self) -> list[UserRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM users ORDER BY created_at, username"
            ).fetchall()
        return [_record(row) for row in rows]

    # ---- writes ----

    async def create(self, username: str, password: str, role: str = ROLE_MEMBER) -> str | None:
        """Add an account. Returns ``"conflict"`` when the username is taken, else ``None``."""
        encoded = await hash_password(password)
        return await asyncio.to_thread(
            self._create, normalize_username(username), encoded, role, int(time.time())
        )

    def _create(self, username: str, password_hash: str, role: str, now: int) -> str | None:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, role, state, password_hash, created_at, last_login_at)"
                " VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT(username) DO NOTHING",
                (username, role, STATE_ACTIVE, password_hash, now),
            )
        return None if cursor.rowcount == 1 else "conflict"

    async def set_password(self, username: str, password: str) -> bool:
        encoded = await hash_password(password)
        return await asyncio.to_thread(
            self._update, normalize_username(username), "password_hash", encoded
        )

    async def set_state(self, username: str, state: str) -> bool:
        return await asyncio.to_thread(self._update, normalize_username(username), "state", state)

    async def set_role(self, username: str, role: str) -> bool:
        return await asyncio.to_thread(self._update, normalize_username(username), "role", role)

    def _update(self, username: str, column: str, value: Any) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE users SET {column}=? WHERE username=?", (value, username)
            )
        return cursor.rowcount == 1

    async def touch_login(self, username: str, *, now: int | None = None) -> None:
        await asyncio.to_thread(
            self._update,
            normalize_username(username),
            "last_login_at",
            int(time.time()) if now is None else now,
        )

    async def delete(self, username: str) -> bool:
        return await asyncio.to_thread(self._delete, normalize_username(username))

    def _delete(self, username: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM users WHERE username=?", (username,))
        return cursor.rowcount == 1

    # ---- registration ----

    async def registration_open(self) -> bool:
        return await asyncio.to_thread(self._registration_open)

    def _registration_open(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?", (REGISTRATION_KEY,)
            ).fetchone()
        return row is not None and str(row["value"]) == "1"

    async def set_registration_open(self, open_: bool) -> None:
        await asyncio.to_thread(self._set_registration_open, open_)

    def _set_registration_open(self, open_: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (REGISTRATION_KEY, "1" if open_ else "0"),
            )


_COLUMNS = "username, role, state, password_hash, created_at, last_login_at"


def _record(row: Any) -> UserRecord:
    return UserRecord(
        username=str(row["username"]),
        role=str(row["role"]),
        state=str(row["state"]),
        created_at=int(row["created_at"]),
        last_login_at=None if row["last_login_at"] is None else int(row["last_login_at"]),
        password_hash=None if row["password_hash"] is None else str(row["password_hash"]),
    )
