"""Durable registration stores for Web Push and APNs.

Adapted from cc-remote's ``relay/push.py`` and ``relay/native_push.py`` (MIT, see
THIRD_PARTY_NOTICES.md). Rows are bound to the session that created them so signing out stops the
notifications that login started, and to the account that signed in so a transition on one
person's device never reaches another's phone (A24).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounts import ADMIN_USERNAME
from .migrations import Migration, apply_migrations

#: Columns added since these tables' first release; see rc_gateway/migrations.py. Registrations
#: made before accounts existed are the admin's, which is what the gateway had at the time (A24).
MIGRATIONS: tuple[Migration, ...] = (
    ("web_push", "username", f"TEXT NOT NULL DEFAULT '{ADMIN_USERNAME}'"),
    ("apns_tokens", "username", f"TEXT NOT NULL DEFAULT '{ADMIN_USERNAME}'"),
    # A47: a delivery that only changes the badge goes out at a lower priority, and a newer one
    # replaces it while it waits.
    ("apns_deliveries", "badge_only", "INTEGER NOT NULL DEFAULT 0"),
)


@dataclass(frozen=True)
class WebPushSubscription:
    endpoint: str
    p256dh: str
    auth: str
    session_jti: str
    username: str
    expires_at: float

    def browser_payload(self) -> dict[str, Any]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}


@dataclass(frozen=True)
class ApnsRegistration:
    device_token: str
    environment: str
    bundle_id: str
    session_jti: str
    username: str
    expires_at: float


@dataclass(frozen=True)
class ApnsDelivery:
    delivery_id: int
    device_token: str
    environment: str
    payload: str
    attempts: int
    due_at: float
    #: A47: the notification shows nothing and only sets the app icon's badge.
    badge_only: bool


class PushStore:
    """One SQLite file holding Web Push subscriptions, APNs tokens and the delivery journal."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # Session titles, working directories and push endpoints are not world-readable.
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
                CREATE TABLE IF NOT EXISTS web_push (
                    endpoint TEXT PRIMARY KEY,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    session_jti TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT 'admin',
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS apns_tokens (
                    device_token TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    session_jti TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT 'admin',
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS apns_deliveries (
                    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_token TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    badge_only INTEGER NOT NULL DEFAULT 0,
                    due_at REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS apns_deliveries_due_idx ON apns_deliveries(due_at)"
            )
            apply_migrations(connection, MIGRATIONS)

    # ---- web push ----

    async def upsert_web(self, subscription: WebPushSubscription) -> bool:
        """Store one browser subscription. False when the endpoint belongs to another account."""
        return await asyncio.to_thread(self._upsert_web, subscription)

    def _upsert_web(self, subscription: WebPushSubscription) -> bool:
        now = time.time()
        with self._connect() as connection:
            # A row whose login session has expired is nobody's any more, so the next owner of
            # this browser is not blocked behind it until a delivery happens to prune it.
            connection.execute(
                "DELETE FROM web_push WHERE endpoint=? AND expires_at<=?",
                (subscription.endpoint, now),
            )
            cursor = connection.execute(
                """
                INSERT INTO web_push(
                    endpoint, p256dh, auth, session_jti, username, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh=excluded.p256dh, auth=excluded.auth,
                    session_jti=excluded.session_jti,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at
                WHERE web_push.username=excluded.username
                """,
                (
                    subscription.endpoint,
                    subscription.p256dh,
                    subscription.auth,
                    subscription.session_jti,
                    subscription.username,
                    subscription.expires_at,
                    now,
                ),
            )
            # A row never changes owner: re-registering someone else's endpoint would otherwise
            # move their notifications to the caller and deliver the caller's sessions to their
            # browser. The real owner's row has to expire or be removed first (A24).
            return cursor.rowcount > 0

    async def remove_web(self, endpoint: str, username: str) -> None:
        """Remove one browser subscription, and only if it is this account's (A24)."""
        await asyncio.to_thread(self._remove_web, endpoint, username)

    def _remove_web(self, endpoint: str, username: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM web_push WHERE endpoint=? AND username=?", (endpoint, username)
            )

    async def list_web(self, username: str) -> list[WebPushSubscription]:
        """One account's browser subscriptions: a transition never reaches another's (A24)."""
        return await asyncio.to_thread(self._list_web, username)

    def _list_web(self, username: str) -> list[WebPushSubscription]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM web_push WHERE expires_at<=?", (now,))
            rows = connection.execute(
                "SELECT endpoint, p256dh, auth, session_jti, username, expires_at "
                "FROM web_push WHERE username=?",
                (username,),
            ).fetchall()
        return [
            WebPushSubscription(
                endpoint=str(row["endpoint"]),
                p256dh=str(row["p256dh"]),
                auth=str(row["auth"]),
                session_jti=str(row["session_jti"]),
                username=str(row["username"]),
                expires_at=float(row["expires_at"]),
            )
            for row in rows
        ]

    # ---- apns ----

    async def upsert_apns(self, registration: ApnsRegistration) -> bool:
        """Store one phone. False when the device token is already another account's."""
        return await asyncio.to_thread(self._upsert_apns, registration)

    def _upsert_apns(self, registration: ApnsRegistration) -> bool:
        now = time.time()
        with self._connect() as connection:
            # Same reason as `_upsert_web`: an expired registration holds nobody's phone.
            connection.execute(
                "DELETE FROM apns_tokens WHERE device_token=? AND expires_at<=?",
                (registration.device_token, now),
            )
            cursor = connection.execute(
                """
                INSERT INTO apns_tokens(
                    device_token, environment, bundle_id, session_jti, username, expires_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_token) DO UPDATE SET
                    environment=excluded.environment, bundle_id=excluded.bundle_id,
                    session_jti=excluded.session_jti,
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at
                WHERE apns_tokens.username=excluded.username
                """,
                (
                    registration.device_token,
                    registration.environment,
                    registration.bundle_id,
                    registration.session_jti,
                    registration.username,
                    registration.expires_at,
                    now,
                ),
            )
            # Same rule as `_upsert_web`: a row never changes owner.
            return cursor.rowcount > 0

    async def remove_apns(self, device_token: str, username: str | None = None) -> None:
        """Remove one phone's registration.

        ``username`` scopes the delete to one account and is what every route passes (A24).
        ``None`` is for the gateway's own cleanup, where APNs itself said the token is dead and
        the owner is beside the point.
        """
        await asyncio.to_thread(self._remove_apns, device_token, username)

    def _remove_apns(self, device_token: str, username: str | None) -> None:
        with self._connect() as connection:
            if username is None:
                cursor = connection.execute(
                    "DELETE FROM apns_tokens WHERE device_token=?", (device_token,)
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM apns_tokens WHERE device_token=? AND username=?",
                    (device_token, username),
                )
            # The queued deliveries go with the registration, and stay when it was not this
            # caller's to remove: dropping them would be another account's outage.
            if cursor.rowcount > 0:
                connection.execute(
                    "DELETE FROM apns_deliveries WHERE device_token=?", (device_token,)
                )

    async def list_apns(self, username: str) -> list[ApnsRegistration]:
        """One account's phones, for the same reason ``list_web`` takes a username (A24)."""
        return await asyncio.to_thread(self._list_apns, username)

    def _list_apns(self, username: str) -> list[ApnsRegistration]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("DELETE FROM apns_tokens WHERE expires_at<=?", (now,))
            rows = connection.execute(
                "SELECT device_token, environment, bundle_id, session_jti, username, expires_at "
                "FROM apns_tokens WHERE username=?",
                (username,),
            ).fetchall()
        return [
            ApnsRegistration(
                device_token=str(row["device_token"]),
                environment=str(row["environment"]),
                bundle_id=str(row["bundle_id"]),
                session_jti=str(row["session_jti"]),
                username=str(row["username"]),
                expires_at=float(row["expires_at"]),
            )
            for row in rows
        ]

    async def revoke_session(self, jti: str) -> None:
        """Sign-out removes every registration that login created."""
        await asyncio.to_thread(self._revoke_session, jti)

    def _revoke_session(self, jti: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM web_push WHERE session_jti=?", (jti,))
            connection.execute("DELETE FROM apns_tokens WHERE session_jti=?", (jti,))

    async def remove_for_user(self, username: str) -> None:
        """A deleted account takes its notifications with it (A24)."""
        await asyncio.to_thread(self._remove_for_user, username)

    def _remove_for_user(self, username: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM web_push WHERE username=?", (username,))
            connection.execute(
                "DELETE FROM apns_deliveries WHERE device_token IN "
                "(SELECT device_token FROM apns_tokens WHERE username=?)",
                (username,),
            )
            connection.execute("DELETE FROM apns_tokens WHERE username=?", (username,))

    # ---- delivery journal ----

    async def enqueue(
        self, device_token: str, environment: str, payload: str, *, badge_only: bool = False
    ) -> None:
        await asyncio.to_thread(self._enqueue, device_token, environment, payload, badge_only)

    def _enqueue(self, device_token: str, environment: str, payload: str, badge_only: bool) -> None:
        now = time.time()
        with self._connect() as connection:
            # A47: every push carries the account's newest count, so a badge-only delivery still
            # waiting for this phone could only set an older one after it.
            connection.execute(
                "DELETE FROM apns_deliveries WHERE device_token=? AND badge_only=1", (device_token,)
            )
            connection.execute(
                "INSERT INTO apns_deliveries("
                "device_token, environment, payload, badge_only, due_at, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (device_token, environment, payload, 1 if badge_only else 0, now, now),
            )

    async def claim_due(self, limit: int = 32) -> list[ApnsDelivery]:
        return await asyncio.to_thread(self._claim_due, limit)

    def _claim_due(self, limit: int) -> list[ApnsDelivery]:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT delivery_id, device_token, environment, payload, attempts, due_at, "
                "badge_only FROM apns_deliveries WHERE due_at<=? ORDER BY due_at LIMIT ?",
                (now, limit),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE apns_deliveries SET attempts=attempts+1, due_at=? WHERE delivery_id=?",
                    (now + 300, int(row["delivery_id"])),
                )
            connection.commit()
        finally:
            connection.close()
        return [
            ApnsDelivery(
                delivery_id=int(row["delivery_id"]),
                device_token=str(row["device_token"]),
                environment=str(row["environment"]),
                payload=str(row["payload"]),
                attempts=int(row["attempts"]),
                due_at=float(row["due_at"]),
                badge_only=bool(row["badge_only"]),
            )
            for row in rows
        ]

    async def finish(self, delivery_id: int) -> None:
        await asyncio.to_thread(self._finish, delivery_id)

    def _finish(self, delivery_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM apns_deliveries WHERE delivery_id=?", (delivery_id,))

    async def retry_at(self, delivery_id: int, when: float) -> None:
        await asyncio.to_thread(self._retry_at, delivery_id, when)

    def _retry_at(self, delivery_id: int, when: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE apns_deliveries SET due_at=? WHERE delivery_id=?", (when, delivery_id)
            )

    async def pending_count(self) -> int:
        return await asyncio.to_thread(self._pending_count)

    def _pending_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM apns_deliveries").fetchone()[0])
