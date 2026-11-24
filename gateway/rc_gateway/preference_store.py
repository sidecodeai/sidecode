"""The preferences an account carries, stored on the gateway rather than in an app (A35, A41).

A preference is a choice that must read the same in the browser, on the phone and on every device
of the account — the browser cannot own it, because the device has to act on it while no app is
running. The file follows ``push_store.py``: its own SQLite database under ``DATA_DIR``, opened at
0600, with additive migrations for anything a later amendment adds.

An account with no row has never touched a preference and reads the defaults, so a fresh gateway
needs no seeding and a deleted account leaves nothing behind. A41 added the Settings screen's own
preferences beside A35's switch; each of them is absent until somebody sets it, so an app can tell
"nobody has chosen" from "chosen, and this is the value", and write its own up the first time.

Beside the accounts the file keeps the task board provider's name the permissions were last given
under (A65), so a gateway that starts under another name can ask every account again.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .migrations import Migration, apply_migrations

#: Columns added since this table's first release; see rc_gateway/migrations.py. A deployment
#: started before A41 has the switch alone, and gains the Settings preferences here; one started
#: before A62 gains the task board's permission.
MIGRATIONS: tuple[Migration, ...] = (
    ("preferences", "language", "TEXT"),
    ("preferences", "stt_language", "TEXT"),
    ("preferences", "polish_enabled", "INTEGER"),
    ("preferences", "polish_model", "TEXT"),
    ("preferences", "polish_strength", "TEXT"),
    ("preferences", "timeline_detail", "TEXT"),
    ("preferences", "task_board", "INTEGER"),
)

#: The preferences that are absent until they are set, in the order the schema lists them.
OPTIONAL_FIELDS: tuple[str, ...] = (
    "language",
    "stt_language",
    "polish_enabled",
    "polish_model",
    "polish_strength",
    "timeline_detail",
    "task_board",
)

#: Every preference the wire knows, so a PATCH body is validated against one list.
FIELDS: tuple[str, ...] = ("resume_after_limit", *OPTIONAL_FIELDS)

#: The preferences only the gateway and the apps act on: a device is never sent them (A62).
APP_ONLY_FIELDS: frozenset[str] = frozenset({"task_board"})


@dataclass(frozen=True)
class Preferences:
    """One account's preferences, as ``objects.json#/$defs/Preferences`` (A35, A41)."""

    #: Whether a session the vendor's usage limit stopped is resumed by its device once the limit
    #: resets (PROTOCOL 7.2). Off until the person turns it on.
    resume_after_limit: bool = False
    #: The app's interface language: ``en`` or ``zh-Hans``.
    language: str | None = None
    #: The language a phone that recognises speech itself listens for — ``zh``, ``en``, … —
    #: and while it is unset the app's interface language decides: ``en`` for English, ``zh`` for
    #: 简体中文 (A66); an ``auto`` written before A44 reads as unset. The gateway's own provider
    #: detects the language and never reads it; this stores what an app writes.
    stt_language: str | None = None
    #: Whether a finished dictation goes through the gateway's polish model (A29).
    polish_enabled: bool | None = None
    #: The polish model chosen from ``GET /api/polish/models``; empty when none.
    polish_model: str | None = None
    #: How far the polish may go: ``moderate`` or ``strong`` (A29).
    polish_strength: str | None = None
    #: How much of a transcript is drawn: ``simple`` or ``detailed``.
    timeline_detail: str | None = None
    #: Whether the gateway keeps task boards for the account (A62). Absent reads as off, and only
    #: the person's turning the board on or off writes it; an app never writes it from its own.
    task_board: bool | None = None

    def view(self) -> dict[str, Any]:
        """The wire object: the switch always, and every preference somebody has set."""
        view: dict[str, Any] = {"resume_after_limit": self.resume_after_limit}
        for name in OPTIONAL_FIELDS:
            value = getattr(self, name)
            if value is not None:
                view[name] = value
        return view

    def device_view(self) -> dict[str, Any]:
        """What a device is sent: the wire object without the preferences only apps read."""
        return {name: value for name, value in self.view().items() if name not in APP_ONLY_FIELDS}

    @property
    def board_on(self) -> bool:
        """Whether the account's task board is on: set, and set to true (A62)."""
        return self.task_board is True


DEFAULTS = Preferences()

#: Built from ``FIELDS`` so the statements cannot drift from the object a later amendment grows.
_COLUMNS = ", ".join(FIELDS)
_SELECT = f"SELECT {_COLUMNS} FROM preferences WHERE username=?"
_UPSERT = f"""
    INSERT INTO preferences(username, {_COLUMNS}, updated_at)
    VALUES (?, {", ".join("?" for _ in FIELDS)}, ?)
    ON CONFLICT(username) DO UPDATE SET
        {", ".join(f"{name}=excluded.{name}" for name in FIELDS)},
        updated_at=excluded.updated_at
"""

#: The key of the task board provider's name in ``preference_meta`` (A65).
BOARD_PROVIDER_KEY = "board_provider_name"


class PreferenceStore:
    """One SQLite file holding one row per account."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # A preference names an account, so the file is no more readable than the others.
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
                CREATE TABLE IF NOT EXISTS preferences (
                    username TEXT PRIMARY KEY,
                    resume_after_limit INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    language TEXT,
                    stt_language TEXT,
                    polish_enabled INTEGER,
                    polish_model TEXT,
                    polish_strength TEXT,
                    timeline_detail TEXT,
                    task_board INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS preference_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            apply_migrations(connection, MIGRATIONS)

    async def get(self, username: str) -> Preferences:
        """One account's preferences. An account with no row reads the defaults."""
        return await asyncio.to_thread(self._get, username)

    def _get(self, username: str) -> Preferences:
        with self._connect() as connection:
            row = connection.execute(_SELECT, (username,)).fetchone()
        return _from_row(row)

    async def patch(self, username: str, values: Mapping[str, Any]) -> Preferences:
        """Set the preferences named in ``values`` and return the whole object afterwards."""
        return await asyncio.to_thread(self._patch, username, values)

    def _patch(self, username: str, values: Mapping[str, Any]) -> Preferences:
        # Read and write in one transaction: two apps setting different preferences at once must
        # not have the second overwrite the first with a value it read before the change.
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT, (username,)).fetchone()
            merged = replace(_from_row(row), **dict(values))
            connection.execute(_UPSERT, (username, *_to_row(merged), time.time()))
            connection.commit()
        finally:
            connection.close()
        return merged

    async def remove_for_user(self, username: str) -> None:
        """A deleted account takes its preferences with it, as its registrations go (A24)."""
        await asyncio.to_thread(self._remove_for_user, username)

    def _remove_for_user(self, username: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM preferences WHERE username=?", (username,))

    async def board_accounts(self) -> list[str]:
        """The accounts whose task board is on (A62)."""
        return await asyncio.to_thread(self._board_accounts)

    def _board_accounts(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username FROM preferences WHERE task_board=1 ORDER BY username"
            ).fetchall()
        return [str(row["username"]) for row in rows]

    async def board_provider(self) -> str:
        """The task board provider's name the permissions were last given under, "" for none
        (A65). A gateway that never recorded one had none."""
        return await asyncio.to_thread(self._board_provider)

    def _board_provider(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM preference_meta WHERE key=?", (BOARD_PROVIDER_KEY,)
            ).fetchone()
        return "" if row is None else str(row["value"])

    async def record_board_provider(self, name: str) -> None:
        await asyncio.to_thread(self._record_board_provider, name)

    def _record_board_provider(self, name: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO preference_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (BOARD_PROVIDER_KEY, name),
            )


def _from_row(row: sqlite3.Row | None) -> Preferences:
    if row is None:
        return DEFAULTS
    return Preferences(
        resume_after_limit=bool(row["resume_after_limit"]),
        language=_text(row["language"]),
        stt_language=_text(row["stt_language"]),
        polish_enabled=_flag(row["polish_enabled"]),
        polish_model=_text(row["polish_model"]),
        polish_strength=_text(row["polish_strength"]),
        timeline_detail=_text(row["timeline_detail"]),
        task_board=_flag(row["task_board"]),
    )


def _to_row(preferences: Preferences) -> tuple[Any, ...]:
    """The stored values in ``FIELDS`` order; a boolean goes in as the integer it reads back as."""
    return tuple(_to_column(getattr(preferences, name)) for name in FIELDS)


def _to_column(value: Any) -> Any:
    return int(value) if isinstance(value, bool) else value


def _text(value: Any) -> str | None:
    """A column nobody has written reads as NULL, which is "unset" rather than the empty string."""
    return None if value is None else str(value)


def _flag(value: Any) -> bool | None:
    return None if value is None else bool(value)
