"""Device enrollment: hashed pairing codes and hashed device tokens.

Adapted from cc-remote's ``cc_remote/relay/devices.py`` (MIT, see THIRD_PARTY_NOTICES.md). Only
SHA-256 digests are stored, redemption runs inside ``BEGIN IMMEDIATE`` so a code can be spent
exactly once, and revocation deletes the row so the token stops working before the next reconnect.

Pairing code format is ``RC-XXXX-XXXX`` over the Crockford base32 alphabet without I, L, O and U.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .migrations import Migration, apply_migrations

#: Columns added since the first release; see rc_gateway/migrations.py.
MIGRATIONS: tuple[Migration, ...] = (
    ("devices", "agents", "TEXT NOT NULL DEFAULT '[]'"),
    ("pairing_codes", "redeemed_at", "INTEGER"),
    ("devices", "client_build", "TEXT"),
    ("devices", "update_state", "TEXT NOT NULL DEFAULT 'idle'"),
    ("devices", "update_message", "TEXT"),
    ("devices", "update_failed_build", "TEXT"),
    # A38: nullable on purpose. NULL is "this client never said", which the `Device` object
    # reports as an absent field rather than as a device without a shell.
    ("devices", "terminal", "INTEGER"),
)

PAIR_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_BODY_LENGTH = 8
MAX_ACTIVE_DEVICES = 64
MIN_TOKEN_LENGTH = 32

#: ``Device.update_state`` (A22). A device is ``idle`` until an app asks it to update.
UPDATE_IDLE = "idle"
UPDATE_RUNNING = "updating"
UPDATE_FAILED = "failed"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def generate_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(CODE_BODY_LENGTH))
    return f"RC-{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(value: str) -> str:
    """Reduce a user-typed code to its alphabet characters, dropping the ``RC`` prefix."""
    body = value.upper()
    if body.startswith("RC-"):
        body = body[3:]
    return "".join(char for char in body if char in PAIR_ALPHABET)


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    #: The account that paired this machine (A24). Never part of the `Device` object an app sees.
    username: str
    name: str
    platform: str
    hostname: str
    arch: str
    client_version: str
    created_at: int
    last_seen: int | None
    agents: list[dict[str, Any]] = field(default_factory=list)
    #: A22: the wheel the client was installed from, and the update an app asked for.
    client_build: str | None = None
    update_state: str = UPDATE_IDLE
    update_message: str | None = None
    #: A36, gateway-internal and never part of the `Device` an app sees: the served build that
    #: failed on this machine, so the gateway does not try that same wheel again by itself.
    update_failed_build: str | None = None
    #: A38: whether the machine offers a shell, as its `hello` said. None on a client older than
    #: the amendment, which never says either way.
    terminal: bool | None = None


@dataclass(frozen=True)
class PairingGrant:
    code: str
    expires_at: int


@dataclass(frozen=True)
class EnrolledDevice:
    device_id: str
    token: str
    name: str


class DeviceStore:
    """SQLite registry shared by HTTP enrollment and WebSocket authentication."""

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
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    arch TEXT NOT NULL,
                    client_version TEXT NOT NULL,
                    token_hash BLOB NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER,
                    agents TEXT NOT NULL DEFAULT '[]',
                    client_build TEXT,
                    update_state TEXT NOT NULL DEFAULT 'idle',
                    update_message TEXT,
                    update_failed_build TEXT,
                    terminal INTEGER
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS devices_username_idx ON devices(username)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code_hash BLOB PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    redeemed_at INTEGER
                )
                """
            )
            apply_migrations(connection, MIGRATIONS)

    async def create_pairing(
        self, username: str, *, ttl: int = 600, now: int | None = None
    ) -> PairingGrant:
        return await asyncio.to_thread(self._create_pairing, username, ttl, _now(now))

    def _create_pairing(self, username: str, ttl: int, now: int) -> PairingGrant:
        code = generate_pairing_code()
        expires_at = now + ttl
        with self._connect() as connection:
            # Spent codes are kept until they expire so a replay reads as a conflict.
            connection.execute("DELETE FROM pairing_codes WHERE expires_at<=?", (now,))
            connection.execute(
                "INSERT INTO pairing_codes(code_hash, username, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_digest(normalize_pairing_code(code)), username, now, expires_at),
            )
        return PairingGrant(code=code, expires_at=expires_at)

    async def cancel_pairing(self, code: str, username: str) -> bool:
        return await asyncio.to_thread(self._cancel_pairing, code, username)

    def _cancel_pairing(self, code: str, username: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pairing_codes "
                "WHERE code_hash=? AND username=? AND redeemed_at IS NULL",
                (_digest(normalize_pairing_code(code)), username),
            )
        return cursor.rowcount == 1

    async def redeem(
        self,
        code: str,
        *,
        name: str,
        platform: str,
        hostname: str,
        arch: str,
        client_version: str,
        agents: list[dict[str, Any]] | None = None,
        now: int | None = None,
    ) -> EnrolledDevice | str:
        """Spend a pairing code. Returns the enrolled device or an error code string."""
        return await asyncio.to_thread(
            self._redeem,
            normalize_pairing_code(code),
            name,
            platform,
            hostname,
            arch,
            client_version,
            agents,
            _now(now),
        )

    def _redeem(
        self,
        code: str,
        name: str,
        platform: str,
        hostname: str,
        arch: str,
        client_version: str,
        agents: list[dict[str, Any]] | None,
        now: int,
    ) -> EnrolledDevice | str:
        if len(code) != CODE_BODY_LENGTH:
            return "not_found"
        token = secrets.token_urlsafe(48)
        # protocol/schema/objects.json types device_id as a UUID v4 and every other component
        # validates against it.
        device_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT username, expires_at, redeemed_at FROM pairing_codes WHERE code_hash=?",
                (_digest(code),),
            ).fetchone()
            if row is None:
                connection.rollback()
                return "not_found"
            username = str(row["username"])
            if int(row["expires_at"]) <= now:
                connection.execute("DELETE FROM pairing_codes WHERE code_hash=?", (_digest(code),))
                connection.commit()
                return "not_found"
            if row["redeemed_at"] is not None:
                # The row outlives redemption until it expires, so replaying a spent code is
                # reported as a conflict rather than being indistinguishable from a wrong one.
                connection.rollback()
                return "conflict"
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM devices WHERE username=?", (username,)
                ).fetchone()[0]
            )
            if active >= MAX_ACTIVE_DEVICES:
                connection.rollback()
                return "conflict"
            connection.execute(
                "UPDATE pairing_codes SET redeemed_at=? WHERE code_hash=?", (now, _digest(code))
            )
            connection.execute(
                """
                INSERT INTO devices(
                    device_id, username, name, platform, hostname, arch, client_version,
                    token_hash, created_at, last_seen, agents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    device_id,
                    username,
                    name,
                    platform,
                    hostname,
                    arch,
                    client_version,
                    _digest(token),
                    now,
                    json.dumps(agents or [], ensure_ascii=False, separators=(",", ":")),
                ),
            )
            connection.commit()
            return EnrolledDevice(device_id=device_id, token=token, name=name)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def device_for_token(self, token: str) -> DeviceRecord | None:
        return await asyncio.to_thread(self._device_for_token, token)

    def _device_for_token(self, token: str) -> DeviceRecord | None:
        if len(token) < MIN_TOKEN_LENGTH:
            return None
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM devices WHERE token_hash=?", (_digest(token),)
            ).fetchone()
        return _record(row) if row is not None else None

    async def get(self, device_id: str) -> DeviceRecord | None:
        return await asyncio.to_thread(self._get, device_id)

    def _get(self, device_id: str) -> DeviceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
        return _record(row) if row is not None else None

    async def list_for_user(self, username: str) -> list[DeviceRecord]:
        return await asyncio.to_thread(self._list_for_user, username)

    def _list_for_user(self, username: str) -> list[DeviceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM devices WHERE username=? "
                "ORDER BY COALESCE(last_seen, created_at) DESC, device_id",
                (username,),
            ).fetchall()
        return [_record(row) for row in rows]

    async def count_for_user(self, username: str) -> int:
        return await asyncio.to_thread(self._count_for_user, username)

    def _count_for_user(self, username: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM devices WHERE username=?", (username,)
            ).fetchone()
        return int(row[0])

    async def counts_by_user(self) -> dict[str, int]:
        """How many devices each account has enrolled, for the admin's account list (A24)."""
        return await asyncio.to_thread(self._counts_by_user)

    def _counts_by_user(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, COUNT(*) AS total FROM devices GROUP BY username"
            ).fetchall()
        return {str(row["username"]): int(row["total"]) for row in rows}

    async def touch(self, device_id: str, *, now: int | None = None) -> None:
        await asyncio.to_thread(self._touch, device_id, _now(now))

    def _touch(self, device_id: str, now: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (now, device_id))

    async def describe(
        self,
        device_id: str,
        *,
        agents: list[dict[str, Any]] | None = None,
        terminal: bool | None = None,
        **fields: str,
    ) -> None:
        """Persist the identity, agent inventory and terminal capability a device announced."""
        await asyncio.to_thread(self._describe, device_id, fields, agents, terminal)

    def _describe(
        self,
        device_id: str,
        fields: dict[str, str],
        agents: list[dict[str, Any]] | None,
        terminal: bool | None = None,
    ) -> None:
        allowed = {"name", "platform", "hostname", "arch", "client_version"}
        updates: dict[str, Any] = {
            key: value for key, value in fields.items() if key in allowed and value
        }
        if agents is not None:
            updates["agents"] = json.dumps(agents, ensure_ascii=False, separators=(",", ":"))
        # A38. Only `hello` carries it, and only a client that said something overwrites what is
        # stored: an `agents.updated` never silently takes a device's shell away.
        if terminal is not None:
            updates["terminal"] = int(terminal)
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE devices SET {assignments} WHERE device_id=?",
                (*updates.values(), device_id),
            )

    async def record_build(self, device_id: str, client_build: str | None) -> None:
        """Store the build a ``hello`` reported and end whatever update it concluded (A22, A36).

        A ``hello`` is an update's outcome, and A22 read every one of them as the end of it. Under
        A36 that would hide a failure the moment the old client reconnected, which it does within
        seconds, so a device that comes back running exactly what it ran before keeps its
        ``failed`` state and its message: it did not move, and a person still has to look at it. A
        device on any other build did move, and takes the whole failure with it.
        """
        await asyncio.to_thread(self._record_build, device_id, client_build)

    def _record_build(self, device_id: str, client_build: str | None) -> None:
        build = client_build or None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT client_build, update_failed_build FROM devices WHERE device_id=?",
                (device_id,),
            ).fetchone()
            if (
                row is not None
                and row["update_failed_build"] is not None
                and build == row["client_build"]
            ):
                return
            connection.execute(
                "UPDATE devices SET client_build=?, update_state=?, update_message=NULL, "
                "update_failed_build=NULL WHERE device_id=?",
                (build, UPDATE_IDLE, device_id),
            )

    async def set_update(
        self,
        device_id: str,
        state: str,
        message: str | None = None,
        *,
        failed_build: str | None = None,
    ) -> None:
        """Record that an update is running or has failed (A22), and on which wheel (A36).

        ``failed_build`` follows the state rather than accumulating: an update that starts clears
        the memory of the last one that did not, which is how an app's Retry and a newer wheel
        both let the gateway try again.
        """
        await asyncio.to_thread(self._set_update, device_id, state, message, failed_build)

    def _set_update(
        self, device_id: str, state: str, message: str | None, failed_build: str | None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE devices SET update_state=?, update_message=?, update_failed_build=? "
                "WHERE device_id=?",
                (state, message, failed_build, device_id),
            )

    async def rename(self, device_id: str, username: str, name: str) -> bool:
        return await asyncio.to_thread(self._rename, device_id, username, name)

    def _rename(self, device_id: str, username: str, name: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE devices SET name=? WHERE device_id=? AND username=?",
                (name, device_id, username),
            )
        return cursor.rowcount == 1

    async def revoke(self, device_id: str, username: str) -> bool:
        return await asyncio.to_thread(self._revoke, device_id, username)

    def _revoke(self, device_id: str, username: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM devices WHERE device_id=? AND username=?", (device_id, username)
            )
        return cursor.rowcount == 1

    async def drop_pairings_for_user(self, username: str) -> int:
        """Forget an account's outstanding codes, so a deleted account's code cannot enrol."""
        return await asyncio.to_thread(self._drop_pairings_for_user, username)

    def _drop_pairings_for_user(self, username: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM pairing_codes WHERE username=?", (username,))
        return int(cursor.rowcount)


_COLUMNS = (
    "device_id, username, name, platform, hostname, arch, client_version, created_at, last_seen, "
    "agents, client_build, update_state, update_message, update_failed_build, terminal"
)


def _record(row: Any) -> DeviceRecord:
    return DeviceRecord(
        device_id=str(row["device_id"]),
        username=str(row["username"]),
        name=str(row["name"]),
        platform=str(row["platform"]),
        hostname=str(row["hostname"]),
        arch=str(row["arch"]),
        client_version=str(row["client_version"]),
        created_at=int(row["created_at"]),
        last_seen=None if row["last_seen"] is None else int(row["last_seen"]),
        agents=_agents(row["agents"]),
        client_build=None if row["client_build"] is None else str(row["client_build"]),
        update_state=str(row["update_state"] or UPDATE_IDLE),
        update_message=None if row["update_message"] is None else str(row["update_message"]),
        update_failed_build=(
            None if row["update_failed_build"] is None else str(row["update_failed_build"])
        ),
        terminal=None if row["terminal"] is None else bool(row["terminal"]),
    )


def _agents(raw: Any) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else value
