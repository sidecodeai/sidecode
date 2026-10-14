"""SQLite registry for sessions, history events and `session.send` idempotency.

Writes are queued and applied in batches by a single worker, because a commit
is a disk flush and one per published event put the device's event loop on the
disk's clock: a busy turn stalled the loop long enough for the gateway to call
the link dead. Reads apply whatever is queued before they run, so nothing ever
observes a write that has not landed, and the queue drains inline when there is
no event loop to run a worker on.

`seq` allocation stays synchronous: it is handed out from memory, in order,
seeded from the database the first time a session asks. The counter is written
back with the events it numbered, so a batch lost to a kill takes its events
and its counter together and no `seq` is ever handed out twice.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .events import bound_block, dedup_key
from .logging_setup import logger
from .models import Session, now_ms

log = logger("rc_client.registry")

MAX_EVENTS_PER_SESSION = 20_000
PRUNE_EVERY = 500
REQUEST_TTL_MS = 24 * 60 * 60 * 1000
# How long a batch may wait for company. Short enough that a kill loses almost
# nothing, long enough that a streaming turn commits once rather than per event.
FLUSH_DELAY = 0.05
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent      TEXT NOT NULL,
    data       TEXT NOT NULL,
    next_seq   INTEGER NOT NULL DEFAULT 1,
    archived   INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT NOT NULL,
    dedup_key  TEXT NOT NULL,
    order_seq  INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    wire       TEXT NOT NULL,
    full       TEXT,
    PRIMARY KEY (session_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS events_order ON events(session_id, order_seq);
CREATE INDEX IF NOT EXISTS events_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS events_seq ON events(session_id, seq);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    result     TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (session_id, request_id)
);
CREATE TABLE IF NOT EXISTS resumes (
    session_id     TEXT PRIMARY KEY,
    at             INTEGER NOT NULL,
    estimated      INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    window_minutes INTEGER,
    control        TEXT NOT NULL DEFAULT 'remote'
);
"""


class Registry:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._queued: list[tuple[str, tuple[Any, ...]]] = []
        self._prunes: set[str] = set()
        self._seqs: dict[str, int] = {}
        self._since_prune: dict[str, int] = {}
        self._writer: asyncio.Task[None] | None = None
        self._work: asyncio.Event | None = None
        self._closed = False
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()
            # Reclaim pages left behind by pruned events and expired requests.
            self._db.execute("PRAGMA incremental_vacuum")
            self._db.commit()

    def close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.cancel()
        self.flush()
        with self._lock:
            self._closed = True
            self._db.close()

    # ----------------------------------------------------------------- writes

    def _defer(self, sql: str, params: tuple[Any, ...]) -> None:
        """Queue one statement. Order is preserved; the worker commits the batch."""
        with self._lock:
            self._queued.append((sql, params))
        if not self._start_writer():
            self.flush()

    def _start_writer(self) -> bool:
        """Wake the writer, starting it if this is the first deferred write.

        False means there is no event loop to run one on — a CLI command, or a
        test — and the caller applies the queue itself.
        """
        if self._closed:
            return False
        if self._writer is not None and not self._writer.done():
            if self._work is not None:
                self._work.set()
            return True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._work = asyncio.Event()
        self._work.set()
        self._writer = asyncio.create_task(self._write_loop(), name="registry-writer")
        return True

    async def _write_loop(self) -> None:
        """One worker, one batch at a time, off the event loop."""
        work = self._work
        assert work is not None
        while True:
            await work.wait()
            work.clear()
            await asyncio.sleep(FLUSH_DELAY)
            try:
                await asyncio.to_thread(self.flush)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("registry write failed")

    def flush(self) -> None:
        """Apply everything queued, in order, in one transaction."""
        with self._lock:
            if self._closed or (not self._queued and not self._prunes):
                return
            batch, self._queued = self._queued, []
            prunes, self._prunes = self._prunes, set()
            for sql, params in batch:
                self._db.execute(sql, params)
            for session_id in sorted(prunes):
                self._prune(session_id)
            self._db.commit()

    # ---------------------------------------------------------------- sessions

    def upsert_session(self, session: Session) -> None:
        self._defer(
            """
            INSERT INTO sessions (session_id, agent, data, next_seq, archived, updated_at)
            VALUES (?, ?, ?, COALESCE(
                (SELECT next_seq FROM sessions WHERE session_id = ?), 1), ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                agent = excluded.agent, data = excluded.data,
                archived = excluded.archived, updated_at = excluded.updated_at
            """,
            (
                session.session_id,
                session.agent,
                json.dumps(session.to_dict(), ensure_ascii=False),
                session.session_id,
                1 if session.archived else 0,
                session.updated_at,
            ),
        )

    def load_sessions(self) -> list[Session]:
        """Every decodable session row; a damaged one is skipped, not fatal."""
        self.flush()
        with self._lock:
            rows = self._db.execute("SELECT data FROM sessions ORDER BY updated_at DESC").fetchall()
        sessions: list[Session] = []
        for row in rows:
            try:
                payload = json.loads(row["data"])
                if not isinstance(payload, dict) or not payload.get("session_id"):
                    continue
                sessions.append(Session.from_dict(payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return sessions

    def delete_session(self, session_id: str) -> None:
        for table in ("sessions", "events", "requests", "resumes"):
            self._defer(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        with self._lock:
            self._seqs.pop(session_id, None)
            self._since_prune.pop(session_id, None)

    def rekey_session(self, old_id: str, new_id: str) -> None:
        """Move a provisional session id to the agent's real one."""
        if old_id == new_id:
            return
        self._defer("DELETE FROM sessions WHERE session_id = ?", (new_id,))
        for table in ("sessions", "events", "requests", "resumes"):
            self._defer(
                f"UPDATE OR REPLACE {table} SET session_id = ? WHERE session_id = ?",
                (new_id, old_id),
            )
        with self._lock:
            # The counter moves with the rows it numbers.
            seq = self._seqs.pop(old_id, None)
            if seq is not None:
                self._seqs[new_id] = seq
            self._since_prune[new_id] = self._since_prune.pop(old_id, 0)

    # ------------------------------------------------------------------- seqs

    def next_seq(self, session_id: str) -> int:
        """The next `seq` for this session, from memory, in order.

        The first call for a session reads the stored counter; every one after
        that is arithmetic. The new value is queued with the event it numbers,
        so the two are persisted or lost together.
        """
        with self._lock:
            seq = self._seqs.get(session_id)
            if seq is None:
                seq = self._stored_seq(session_id)
            self._seqs[session_id] = seq + 1
        self._defer("UPDATE sessions SET next_seq = ? WHERE session_id = ?", (seq + 1, session_id))
        return seq

    def _stored_seq(self, session_id: str) -> int:
        """Where this session's numbering stands on disk, creating a row if needed."""
        self.flush()
        with self._lock:
            row = self._db.execute(
                "SELECT next_seq FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is not None:
            return int(row["next_seq"])
        self._defer(
            "INSERT OR IGNORE INTO sessions"
            " (session_id, agent, data, next_seq, archived, updated_at)"
            " VALUES (?, '', '{}', 1, 0, ?)",
            (session_id, now_ms()),
        )
        return 1

    # ----------------------------------------------------------------- events

    def store_event(self, session_id: str, wire: dict[str, Any], full: dict[str, Any]) -> None:
        """Persist an event if `session.history` should ever replay it."""
        key = dedup_key(wire)
        if key is None:
            return
        wire_json = json.dumps(wire, ensure_ascii=False)
        full_json = json.dumps(bound_block(full), ensure_ascii=False)
        self._defer(
            """
            INSERT INTO events (session_id, dedup_key, order_seq, seq, kind, wire, full)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, dedup_key) DO UPDATE SET
                seq = excluded.seq, kind = excluded.kind,
                wire = excluded.wire, full = excluded.full
            """,
            (
                session_id,
                key,
                int(wire["seq"]),
                int(wire["seq"]),
                str(wire["kind"]),
                wire_json,
                full_json if full_json != wire_json else None,
            ),
        )
        with self._lock:
            written = self._since_prune.get(session_id, 0) + 1
            if written < PRUNE_EVERY:
                self._since_prune[session_id] = written
                return
            self._since_prune[session_id] = 0
            self._prunes.add(session_id)

    def _prune(self, session_id: str) -> None:
        self._prune_to(session_id, MAX_EVENTS_PER_SESSION)

    def _prune_to(self, session_id: str, keep: int) -> None:
        """Drop everything older than the newest `keep` rows.

        Counting the rows first meant walking every one of them on a session
        with a long history. The watermark is the oldest row worth keeping,
        which the `(session_id, order_seq)` index finds by skipping to it, and
        an empty answer deletes nothing.
        """
        with self._lock:
            self._db.execute(
                """
                DELETE FROM events WHERE session_id = ? AND order_seq <= (
                    SELECT order_seq FROM events WHERE session_id = ?
                    ORDER BY order_seq DESC LIMIT 1 OFFSET ?
                )
                """,
                (session_id, session_id, keep),
            )

    def history(
        self,
        session_id: str,
        before_seq: int | None = None,
        limit: int = 200,
        after_seq: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Latest event per block, ascending by `seq` (PROTOCOL.md section 8).

        `seq` is the sequence of a block's most recent version, so a block that is updated later
        moves later in the page. That is what the contract asks for and what `before_seq` pages
        against; `order_seq` only drives pruning, which retires the oldest blocks first.

        `after_seq` (amendment A9) walks the other way, oldest first, so the gateway can backfill
        the events a device produced while the link was down.
        """
        capped = max(1, min(limit, 1000))
        self.flush()
        if after_seq is not None:
            with self._lock:
                rows = self._db.execute(
                    "SELECT wire FROM events WHERE session_id = ? AND seq > ?"
                    " ORDER BY seq ASC LIMIT ?",
                    (session_id, after_seq, capped + 1),
                ).fetchall()
            has_more = len(rows) > capped
            return [json.loads(row["wire"]) for row in rows[:capped]], has_more

        params: list[Any] = [session_id]
        clause = ""
        if before_seq is not None:
            clause = " AND seq < ?"
            params.append(before_seq)
        params.append(capped + 1)
        with self._lock:
            rows = self._db.execute(
                f"SELECT wire, seq FROM events WHERE session_id = ?{clause}"
                " ORDER BY seq DESC LIMIT ?",
                params,
            ).fetchall()
        has_more = len(rows) > capped
        selected = list(reversed(rows[:capped]))
        return [json.loads(row["wire"]) for row in selected], has_more

    def first_seqs(self, session_id: str) -> dict[str, int]:
        """The seq at which each stored block first appeared (amendment A8)."""
        self.flush()
        with self._lock:
            rows = self._db.execute(
                "SELECT dedup_key, order_seq FROM events"
                " WHERE session_id = ? AND dedup_key LIKE 'block:%'",
                (session_id,),
            ).fetchall()
        return {str(row["dedup_key"])[len("block:") :]: int(row["order_seq"]) for row in rows}

    def block(self, session_id: str, block_id: str) -> dict[str, Any] | None:
        self.flush()
        with self._lock:
            row = self._db.execute(
                "SELECT wire, full FROM events WHERE session_id = ? AND dedup_key = ?",
                (session_id, f"block:{block_id}"),
            ).fetchone()
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(row["full"] or row["wire"])
        return payload

    def has_events(self, session_id: str) -> bool:
        """Whether anything worth replaying was ever stored for this session.

        `last_seq` cannot answer this: it counts every event a session was
        given a number for, including the `meta` and `status` ones that are
        published and never stored.
        """
        self.flush()
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM events WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
        return row is not None

    def last_seq(self, session_id: str) -> int:
        self.flush()
        with self._lock:
            row = self._db.execute(
                "SELECT next_seq FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["next_seq"]) - 1 if row else 0

    # ---------------------------------------------------------------- resumes

    def save_resume(
        self,
        session_id: str,
        at: int,
        estimated: bool,
        attempts: int,
        window_minutes: int | None,
        control: str,
    ) -> None:
        """Persist one pending resume, so a restart does not lose it (A35, 7.2)."""
        self._defer(
            "INSERT OR REPLACE INTO resumes"
            " (session_id, at, estimated, attempts, window_minutes, control)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, at, 1 if estimated else 0, attempts, window_minutes, control),
        )

    def load_resumes(self) -> list[dict[str, Any]]:
        """Every stored resume, as the scheduler's own rows."""
        self.flush()
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, at, estimated, attempts, window_minutes, control"
                " FROM resumes ORDER BY at ASC"
            ).fetchall()
        return [
            {
                "session_id": str(row["session_id"]),
                "at": int(row["at"]),
                "estimated": bool(row["estimated"]),
                "attempts": int(row["attempts"]),
                "window_minutes": row["window_minutes"],
                "control": str(row["control"]),
            }
            for row in rows
        ]

    def delete_resume(self, session_id: str) -> None:
        self._defer("DELETE FROM resumes WHERE session_id = ?", (session_id,))

    # -------------------------------------------------------------- key/value

    def get_kv(self, key: str) -> str | None:
        self.flush()
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_kv(self, key: str, value: str) -> None:
        self._defer("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------ idempotency

    def remember_request(self, session_id: str, request_id: str, result: dict[str, Any]) -> None:
        stamp = now_ms()
        self._defer(
            "INSERT OR REPLACE INTO requests (session_id, request_id, result, ts)"
            " VALUES (?, ?, ?, ?)",
            (session_id, request_id, json.dumps(result, ensure_ascii=False), stamp),
        )
        # An idempotency record only has to outlive the app's retries.
        self._defer(
            "DELETE FROM requests WHERE session_id = ? AND ts < ?",
            (session_id, stamp - REQUEST_TTL_MS),
        )

    def recall_request(self, session_id: str, request_id: str) -> dict[str, Any] | None:
        self.flush()
        with self._lock:
            row = self._db.execute(
                "SELECT result FROM requests WHERE session_id = ? AND request_id = ?",
                (session_id, request_id),
            ).fetchone()
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(row["result"])
        return payload
