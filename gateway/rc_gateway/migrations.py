"""Additive SQLite migrations.

``CREATE TABLE IF NOT EXISTS`` is a no-op against a database that already exists, so a column added
to one of those statements never reaches a deployment that has been started once: the table keeps
its old shape and the first query naming the new column fails at runtime. Every store therefore
declares the columns it has added since its first release, and applies the missing ones on open.

Only additive changes belong here. A column is added with a default so existing rows stay valid,
and nothing is ever dropped or renamed.
"""

from __future__ import annotations

import sqlite3

#: ``(table, column, column definition)`` for every column added after a table's first release.
Migration = tuple[str, str, str]


def apply_migrations(connection: sqlite3.Connection, migrations: tuple[Migration, ...]) -> None:
    for table, column, definition in migrations:
        if not _table_exists(connection, table):
            continue
        if column in _columns(connection, table):
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
