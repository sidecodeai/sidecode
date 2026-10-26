"""A47: the mark on a session that stopped working and waits for the person.

The rule is read from the states alone (§4.4): a session works while it is `running`, and waits
while it asks for an approval or an answer, or rests `idle` or `readonly` with something holding
it. Working to waiting marks it; `session.seen`, working again, an explicit close (archived or
stopped, a close on its way to the device included) and removal take it away; every change reaches
the account's sockets and nobody else's, and it survives a restart. A session nothing holds
(`control: none`) keeps its mark and is shown none, because `none` comes and goes: it is a CLI that
exited, and also a terminal not found yet, a pi extension not yet back, a scan that missed. Apps
are shown the mark, and counts include the session, only while it is kept and the session is open
and held.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.badge import BadgeChange
from rc_gateway.connections import AppConnection
from rc_gateway.hub import SessionTransition
from rc_gateway.index import SessionIndex
from rc_gateway.unseen import next_unseen, shows, with_mark

from .conftest import (
    HubRig,
    device_hello,
    drain_until,
    enroll_device,
    fake_app,
    frames_of,
    hub_rig,
    make_config,
    session_summary,
    sign_in,
)

SESSION_ID = "5e551011-2222-4333-8444-555555555555"
OTHER_ID = "5e552022-2222-4333-8444-555555555555"
THIRD_ID = "5e553033-2222-4333-8444-555555555555"
SEEN_ID = "5b0e7d63-0a4f-4c1e-9d1b-7f3e8a2c6b44"
CLOSE_ID = "9b774ca5-6325-4bd0-8545-6e75c45b7ad9"
STATES = (
    "starting",
    "idle",
    "running",
    "needs_approval",
    "needs_input",
    "error",
    "stopped",
    "readonly",
)
CONTROLS = ("remote", "terminal", "shared", "none")


# ---- the rule ----


def _waits(state: str, control: str) -> bool:
    """§4.4 in its own words: the status dot's amber."""
    if state in {"needs_approval", "needs_input"}:
        return True
    return state in {"idle", "readonly"} and control != "none"


def _closed(state: str, archived: bool) -> bool:
    """§4.4 in its own words: an explicit close, which takes the mark away for good."""
    return archived or state == "stopped"


@pytest.mark.parametrize("previous", [None, *STATES])
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("archived", [False, True])
def test_the_rule_over_the_whole_state_table(
    previous: str | None, marked: bool, archived: bool
) -> None:
    for state in STATES:
        for control in CONTROLS:
            summary = {"state": state, "control": control, "archived": archived}
            # Only a running turn is the work a mark follows, and a closed session keeps none (A47).
            if _closed(state, archived) or state == "running":
                expected = False
            elif previous == "running" and _waits(state, control):
                expected = True
            else:
                expected = marked
            assert next_unseen(previous, marked, summary) is expected, (state, control)


@pytest.mark.parametrize(
    ("previous", "state", "control", "expected"),
    [
        # A running turn becoming each waiting state marks: it ended, or it asks something.
        ("running", "idle", "remote", True),
        ("running", "idle", "shared", True),
        ("running", "idle", "terminal", True),
        ("running", "readonly", "terminal", True),
        ("running", "needs_approval", "remote", True),
        ("running", "needs_input", "shared", True),
        # A session that only started has done nothing to look at, whatever it does next.
        ("starting", "idle", "remote", False),
        ("starting", "readonly", "terminal", False),
        ("starting", "needs_approval", "remote", False),
        ("starting", "needs_input", "remote", False),
        # Waiting to waiting does not.
        ("needs_approval", "idle", "remote", False),
        ("idle", "needs_input", "shared", False),
        ("readonly", "idle", "shared", False),
        # Idle with nothing holding it is not waiting: the CLI ended with its turn.
        ("running", "idle", "none", False),
        ("running", "readonly", "none", False),
        # Grey and red dots never bring a red one.
        ("running", "error", "remote", False),
        ("running", "stopped", "none", False),
        ("running", "stopped", "remote", False),
        # A session seen for the first time has no transition to read.
        (None, "needs_approval", "remote", False),
    ],
)
def test_what_marks_a_session(
    previous: str | None, state: str, control: str, expected: bool
) -> None:
    assert next_unseen(previous, False, {"state": state, "control": control}) is expected


@pytest.mark.parametrize(
    ("state", "control", "archived", "expected"),
    [
        ("running", "remote", False, False),
        ("idle", "remote", True, False),
        # A39's close archives, unowned and stopped, in one publish.
        ("stopped", "none", True, False),
        # An agent that stopped is over, held or not.
        ("stopped", "none", False, False),
        ("stopped", "remote", False, False),
        # Nothing else takes it away: a session only starting has not worked again yet, an error
        # keeps it too, and so does one nothing holds, because `none` comes and goes.
        ("starting", "remote", False, True),
        ("error", "remote", False, True),
        ("needs_approval", "remote", False, True),
        ("readonly", "terminal", False, True),
        ("idle", "shared", False, True),
        ("idle", "none", False, True),
        ("readonly", "none", False, True),
        ("error", "none", False, True),
        ("starting", "none", False, True),
    ],
)
def test_what_takes_a_kept_mark_away(
    state: str, control: str, archived: bool, expected: bool
) -> None:
    summary = {"state": state, "control": control, "archived": archived}
    assert next_unseen("idle", True, summary) is expected


@pytest.mark.parametrize("kept", [False, True])
@pytest.mark.parametrize("archived", [False, True])
def test_what_apps_are_shown(kept: bool, archived: bool) -> None:
    """Only a kept mark of a session that is neither closed nor unheld is shown, and counted."""
    for state in STATES:
        for control in CONTROLS:
            summary = {"state": state, "control": control, "archived": archived}
            expected = kept and not _closed(state, archived) and control != "none"
            assert shows(kept, summary) is expected, (state, control)
            assert with_mark(dict(summary), kept).get("unseen", False) is expected, (state, control)


# ---- the index ----


async def test_the_mark_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    index = SessionIndex(path)
    await index.upsert(session_summary(SESSION_ID, "d1", state="running"))
    marked = await index.upsert(session_summary(SESSION_ID, "d1", state="idle"))
    assert marked is not None
    assert marked.unseen is True
    assert marked.summary["unseen"] is True
    await index.close()

    reopened = SessionIndex(path)
    stored = await reopened.get(SESSION_ID)
    assert stored is not None
    assert stored.unseen is True
    assert stored.summary["unseen"] is True
    assert [item.get("unseen") for item in await reopened.list_sessions()] == [True]
    assert await reopened.count_unseen(["d1"]) == 1


async def test_an_index_from_before_the_amendment_is_migrated(tmp_path: Path) -> None:
    """The column is added in place, and a row stored as running marks on its next summary."""
    path = tmp_path / "sessions.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
            last_seq INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
            summary TEXT NOT NULL);
        INSERT INTO sessions VALUES ('s1', 'd1', 'running', 0, 3, 1,
            '{"session_id":"s1","device_id":"d1","state":"running"}');
        """
    )
    legacy.commit()
    legacy.close()

    index = SessionIndex(path)
    stored = await index.get("s1")
    assert stored is not None
    assert stored.unseen is False
    assert "unseen" not in stored.summary
    marked = await index.upsert(session_summary("s1", "d1", state="idle"))
    assert marked is not None
    assert marked.unseen is True


async def test_a_device_can_neither_set_nor_clear_the_mark(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    claimed = await index.upsert(session_summary(SESSION_ID, "d1", unseen=True))
    assert claimed is not None
    assert claimed.unseen is False
    assert "unseen" not in claimed.summary

    await index.upsert(session_summary(SESSION_ID, "d1", state="running"))
    await index.upsert(session_summary(SESSION_ID, "d1", state="idle"))
    kept = await index.upsert(session_summary(SESSION_ID, "d1", state="idle", unseen=False))
    assert kept is not None
    assert kept.unseen is True
    assert kept.summary["unseen"] is True


async def test_a_cleared_mark_is_not_brought_back_by_the_next_summary(tmp_path: Path) -> None:
    """The mark is worked out from the stored row, never from one read before the clear."""
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    await index.upsert(session_summary(SESSION_ID, "d1", state="running"))
    await index.upsert(session_summary(SESSION_ID, "d1", state="idle"))
    assert await index.clear_unseen(SESSION_ID) is not None
    again = await index.upsert(session_summary(SESSION_ID, "d1", state="idle", title="renamed"))
    assert again is not None
    assert again.unseen is False
    assert "unseen" not in again.summary


async def test_the_count_is_the_open_held_marked_sessions_of_the_devices(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    for session_id, device_id in (("s1", "d1"), ("s2", "d1"), ("s3", "d2")):
        await index.upsert(session_summary(session_id, device_id, state="running"))
        await index.upsert(session_summary(session_id, device_id, state="idle"))
    await index.upsert(session_summary("s4", "d1", state="idle"))
    assert await index.count_unseen(["d1"]) == 2
    assert await index.count_unseen(["d1", "d2"]) == 3
    assert await index.count_unseen([]) == 0

    await index.upsert(session_summary("s2", "d1", state="idle", archived=True))
    assert await index.count_unseen(["d1"]) == 1

    cleared = await index.clear_unseen("s1")
    assert cleared is not None
    assert cleared.unseen is False
    assert "unseen" not in cleared.summary
    assert await index.clear_unseen("s1") is None
    assert await index.clear_unseen("nothing") is None
    assert await index.count_unseen(["d1", "d2"]) == 1


async def test_the_count_and_every_list_leave_out_a_session_that_is_closed_or_unheld(
    tmp_path: Path,
) -> None:
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    for session_id in ("open", "archived", "unheld", "stopped"):
        await index.upsert(session_summary(session_id, "d1", state="running"))
        await index.upsert(session_summary(session_id, "d1", state="idle"))
    assert await index.count_unseen(["d1"]) == 4

    closed = {"state": "stopped", "control": "none", "archived": True}
    await index.upsert(session_summary("archived", "d1", **closed))
    # The CLI exited, or no scan has found it: nothing holds the session, which every app files in
    # the Archive.
    await index.upsert(session_summary("unheld", "d1", state="idle", control="none"))
    await index.upsert(session_summary("stopped", "d1", state="stopped"))
    assert await index.count_unseen(["d1"]) == 1
    marks = {item["session_id"]: item.get("unseen") for item in await index.list_sessions()}
    assert marks == {"open": True, "archived": None, "unheld": None, "stopped": None}

    # Closed sessions lost the mark for good; the unheld one only stopped showing it.
    kept = {
        session_id: (await index.get(session_id) or fail()).marked
        for session_id in ("open", "archived", "unheld", "stopped")
    }
    assert kept == {"open": True, "archived": False, "unheld": True, "stopped": False}
    unheld = await index.get("unheld")
    assert unheld is not None
    assert unheld.unseen is False
    assert "unseen" not in unheld.summary


async def test_a_session_that_flaps_to_unheld_and_back_keeps_its_mark(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    await index.upsert(session_summary(SESSION_ID, "d1", state="running"))
    await index.upsert(session_summary(SESSION_ID, "d1", state="readonly", control="terminal"))
    assert await index.count_unseen(["d1"]) == 1

    await index.upsert(session_summary(SESSION_ID, "d1", state="idle", control="none"))
    assert await index.count_unseen(["d1"]) == 0

    held = await index.upsert(
        session_summary(SESSION_ID, "d1", state="readonly", control="terminal")
    )
    assert held is not None
    assert held.unseen is True
    assert held.summary["unseen"] is True
    assert await index.count_unseen(["d1"]) == 1


async def test_an_explicit_close_takes_the_mark_away_for_good(tmp_path: Path) -> None:
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    for session_id in (SESSION_ID, OTHER_ID):
        await index.upsert(session_summary(session_id, "d1", state="running"))
        await index.upsert(session_summary(session_id, "d1", state="idle"))
    await index.upsert(session_summary(SESSION_ID, "d1", state="idle", archived=True))
    await index.upsert(session_summary(OTHER_ID, "d1", state="stopped"))
    assert await index.count_unseen(["d1"]) == 0

    # Written to again (A15) or held by a terminal again: nothing brings a mark back.
    for fields in (
        {"state": "readonly", "control": "terminal"},
        {"state": "idle", "control": "remote"},
    ):
        for session_id in (SESSION_ID, OTHER_ID):
            revived = await index.upsert(session_summary(session_id, "d1", **fields))
            assert revived is not None
            assert revived.marked is False
            assert revived.unseen is False
    assert await index.count_unseen(["d1"]) == 0


def fail() -> Any:
    raise AssertionError("no such session")


async def test_a_summary_stored_while_the_session_is_closing_carries_no_mark(
    tmp_path: Path,
) -> None:
    index = SessionIndex(tmp_path / "sessions.sqlite3")
    await index.upsert(session_summary(SESSION_ID, "d1", state="running"))
    ended = await index.upsert(session_summary(SESSION_ID, "d1", state="idle"), closing=True)
    assert ended is not None
    assert ended.unseen is False
    assert "unseen" not in ended.summary

    await index.upsert(session_summary(OTHER_ID, "d1", state="running"))
    await index.upsert(session_summary(OTHER_ID, "d1", state="idle"))
    assert await index.count_unseen(["d1"]) == 1
    held = await index.upsert(session_summary(OTHER_ID, "d1", state="idle"), closing=True)
    assert held is not None
    assert held.unseen is False
    assert await index.count_unseen(["d1"]) == 0


def _store_marked(path: Path, session_id: str, device_id: str, **fields: Any) -> None:
    """A row with its mark, as a gateway that left a mark on any session stored it."""
    summary = session_summary(session_id, device_id, **fields)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sessions(session_id, device_id, state, archived, unseen, last_seq,"
            " updated_at, summary) VALUES (?, ?, ?, ?, 1, 0, 1, ?)",
            (
                session_id,
                device_id,
                summary["state"],
                int(summary["archived"]),
                json.dumps(summary),
            ),
        )


async def test_a_mark_kept_for_a_closed_session_is_taken_off(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    index = SessionIndex(path)
    _store_marked(path, "open", "d1", state="idle")
    _store_marked(path, "unheld", "d1", state="idle", control="none")
    _store_marked(path, "stopped", "d1", state="stopped")
    _store_marked(path, "archived", "d1", state="stopped", control="none", archived=True)
    assert await index.count_unseen(["d1"]) == 1

    assert await index.clear_closed_unseen() == 2
    kept = {
        session_id: (await index.get(session_id) or fail()).marked
        for session_id in ("open", "unheld", "stopped", "archived")
    }
    # A session nothing holds is no explicit close: it keeps its mark.
    assert kept == {"open": True, "unheld": True, "stopped": False, "archived": False}
    assert await index.count_unseen(["d1"]) == 1
    assert await index.clear_closed_unseen() == 0


# ---- the hub: set, clear and who hears ----


async def _publish(rig: HubRig, session_id: str = SESSION_ID, **fields: Any) -> None:
    await rig.hub.handle_device_frame(
        rig.device,
        {
            "type": "session.updated",
            "session": session_summary(session_id, rig.device_id, **fields),
        },
    )


async def _mark(rig: HubRig, session_id: str = SESSION_ID) -> None:
    await _publish(rig, session_id, state="running")
    await _publish(rig, session_id, state="idle")


def _seen(session_id: str = SESSION_ID, identifier: str = SEEN_ID) -> dict[str, Any]:
    return {"type": "session.seen", "id": identifier, "session_id": session_id}


def _marks(connection: AppConnection) -> list[bool]:
    """The mark on every `session.updated` the socket was sent, in order."""
    return [
        frame["session"].get("unseen", False)
        for frame in frames_of(connection)
        if frame.get("type") == "session.updated"
    ]


async def _rig(tmp_path: Path, changes: list[BadgeChange]) -> HubRig:
    return await hub_rig(tmp_path, on_badge_change=changes.append)


async def test_the_mark_reaches_every_socket_of_the_account_and_no_other(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    phone = fake_app()
    stranger = fake_app("mallory")
    await rig.hub.attach_app(phone)
    await rig.hub.attach_app(stranger)

    await _mark(rig)
    assert _marks(rig.app) == [False, True]
    assert _marks(phone) == [False, True]
    assert frames_of(stranger) == []
    assert changes == [BadgeChange(owner="admin", device_id=rig.device_id, session_id=SESSION_ID)]
    await rig.hub.stop()


async def test_a_turn_that_ended_while_the_device_was_away_marks_on_its_hello(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, [])
    await _publish(rig, state="running")
    frames_of(rig.app)
    hello = device_hello(sessions=[session_summary(SESSION_ID, rig.device_id, state="idle")])
    await rig.hub.handle_device_hello(rig.device, hello)
    assert _marks(rig.app) == [True]
    await rig.hub.stop()


async def test_a_session_first_seen_waiting_is_not_marked(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _publish(rig, state="needs_approval")
    assert _marks(rig.app) == [False]
    assert changes == []
    await rig.hub.stop()


async def test_a_session_created_without_a_first_turn_is_not_marked(tmp_path: Path) -> None:
    """A session that only started has done nothing to look at."""
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _publish(rig, state="starting")
    await _publish(rig, state="idle")
    assert _marks(rig.app) == [False, False]
    assert changes == []
    await rig.hub.stop()


async def test_a_marked_session_starting_again_keeps_its_mark_until_it_runs(
    tmp_path: Path,
) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    frames_of(rig.app)
    await _publish(rig, state="starting")
    await _publish(rig, state="running")
    assert _marks(rig.app) == [True, False]
    assert len(changes) == 2
    await rig.hub.stop()


async def test_session_seen_clears_the_mark_for_every_socket_once(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    phone = fake_app()
    stranger = fake_app("mallory")
    await rig.hub.attach_app(phone)
    await rig.hub.attach_app(stranger)
    await _mark(rig)
    frames_of(rig.app)
    frames_of(phone)

    await rig.hub.handle_app_frame(rig.app, _seen())
    sent = frames_of(rig.app)
    assert sent[0] == {"type": "reply", "id": SEEN_ID, "ok": True, "result": {}}
    assert [frame["type"] for frame in sent[1:]] == ["session.updated"]
    assert "unseen" not in sent[1]["session"]
    assert _marks(phone) == [False]
    assert frames_of(stranger) == []
    assert len(changes) == 2
    stored = await rig.index.get(SESSION_ID)
    assert stored is not None
    assert stored.unseen is False

    # Idempotent: answered again, and nobody else hears about it.
    await rig.hub.handle_app_frame(phone, _seen(identifier=OTHER_ID))
    assert frames_of(phone) == [{"type": "reply", "id": OTHER_ID, "ok": True, "result": {}}]
    assert frames_of(rig.app) == []
    assert len(changes) == 2
    await rig.hub.stop()


async def test_session_seen_clears_a_mark_kept_for_a_session_nothing_holds(
    tmp_path: Path,
) -> None:
    """Nobody was shown the mark, so nobody is told it went; it just does not come back."""
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    await _publish(rig, state="idle", control="none")
    frames_of(rig.app)
    changes.clear()

    await rig.hub.handle_app_frame(rig.app, _seen())
    assert [frame["type"] for frame in frames_of(rig.app)] == ["reply"]
    assert changes == []
    kept = await rig.index.get(SESSION_ID)
    assert kept is not None
    assert kept.marked is False

    await _publish(rig, state="readonly", control="terminal")
    assert _marks(rig.app) == [False]
    await rig.hub.stop()


async def test_session_seen_is_not_found_for_another_account_or_for_nothing(
    tmp_path: Path,
) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    stranger = fake_app("mallory")
    await rig.hub.attach_app(stranger)
    await _mark(rig)
    frames_of(rig.app)
    changes.clear()

    await rig.hub.handle_app_frame(stranger, _seen())
    refused = frames_of(stranger)
    assert [frame["error"]["code"] for frame in refused] == ["not_found"]
    stored = await rig.index.get(SESSION_ID)
    assert stored is not None
    assert stored.unseen is True
    assert frames_of(rig.app) == []
    assert changes == []

    await rig.hub.handle_app_frame(rig.app, _seen("no-such-session"))
    await rig.hub.handle_app_frame(rig.app, {"type": "session.seen", "id": SEEN_ID})
    await rig.hub.handle_app_frame(rig.app, {"type": "session.seen", "session_id": SESSION_ID})
    answered = frames_of(rig.app)
    assert [frame["error"]["code"] for frame in answered] == ["not_found", "bad_request"]
    assert changes == []
    await rig.hub.stop()


async def test_session_seen_is_never_forwarded(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, [])
    await _mark(rig)
    rig.device.drain()
    await rig.hub.handle_app_frame(rig.app, _seen())
    assert frames_of(rig.device) == []
    await rig.hub.stop()


@pytest.mark.parametrize(
    "fields",
    [
        {"state": "running"},
        {"state": "idle", "archived": True},
        # A39's close: archived, unowned and stopped in one publish.
        {"state": "stopped", "control": "none", "archived": True},
    ],
)
async def test_the_mark_clears_when_the_session_works_again_or_is_archived(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    frames_of(rig.app)
    await _publish(rig, **fields)
    assert _marks(rig.app) == [False]
    assert len(changes) == 2
    assert await rig.index.count_unseen([rig.device_id]) == 0
    await rig.hub.stop()


async def test_a_session_nothing_holds_keeps_its_mark_and_shows_it_again_once_held(
    tmp_path: Path,
) -> None:
    """Every app files it in the Archive meanwhile, so no row draws the mark and no count has it."""
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    frames_of(rig.app)
    changes.clear()

    await _publish(rig, state="idle", control="none")
    assert _marks(rig.app) == [False]
    assert len(changes) == 1
    assert await rig.index.count_unseen([rig.device_id]) == 0
    kept = await rig.index.get(SESSION_ID)
    assert kept is not None
    assert (kept.marked, kept.unseen) == (True, False)

    # A scan finds the terminal again, or the pi extension reconnects: the same mark, as it was.
    await _publish(rig, state="readonly", control="terminal")
    assert _marks(rig.app) == [True]
    assert len(changes) == 2
    assert await rig.index.count_unseen([rig.device_id]) == 1
    await rig.hub.stop()


async def test_a_daemon_restart_keeps_the_mark_of_a_terminal_that_is_still_there(
    tmp_path: Path,
) -> None:
    """The first hello of a restarted daemon reports what its first scan found, for each session."""
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig, SESSION_ID)
    await _mark(rig, OTHER_ID)
    frames_of(rig.app)
    changes.clear()

    hello = device_hello(
        sessions=[
            # The CLI is still in its terminal.
            session_summary(SESSION_ID, rig.device_id, state="readonly", control="terminal"),
            # The CLI died while the daemon was down: nothing holds the session, so no app is
            # shown its mark and no count has it.
            session_summary(OTHER_ID, rig.device_id, state="idle", control="none"),
        ]
    )
    await rig.hub.handle_device_hello(rig.device, hello)

    assert _marks(rig.app) == [True, False]
    assert [change.session_id for change in changes] == [OTHER_ID]
    assert await rig.index.count_unseen([rig.device_id]) == 1
    await rig.hub.stop()


async def test_a_pi_session_across_a_daemon_restart_keeps_its_mark(tmp_path: Path) -> None:
    """The extension dials in after the daemon is up, so the hello says `none` until it has."""
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    pi = {"agent": "pi", "origin": "terminal"}
    await _publish(rig, state="running", control="shared", **pi)
    await _publish(rig, state="idle", control="shared", **pi)
    assert _marks(rig.app) == [False, True]
    frames_of(rig.app)

    restarted = device_hello(
        sessions=[session_summary(SESSION_ID, rig.device_id, state="idle", control="none", **pi)]
    )
    await rig.hub.handle_device_hello(rig.device, restarted)
    assert _marks(rig.app) == [False]
    assert await rig.index.count_unseen([rig.device_id]) == 0

    # The extension reconnects, and the session is shared again.
    await _publish(rig, state="idle", control="shared", **pi)
    assert _marks(rig.app) == [True]
    assert await rig.index.count_unseen([rig.device_id]) == 1
    await rig.hub.stop()


async def test_a_session_nothing_holds_is_not_marked_when_its_turn_ends(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _publish(rig, state="running")
    await _publish(rig, state="idle", control="none")
    assert _marks(rig.app) == [False, False]
    assert changes == []
    await rig.hub.stop()


async def test_the_gateway_taking_its_marks_off_closed_sessions_when_it_starts(
    tmp_path: Path,
) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _publish(rig, SESSION_ID, state="stopped")
    await _publish(rig, OTHER_ID, state="idle")
    await _publish(rig, THIRD_ID, state="idle", control="none")
    path = rig.index.path
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET unseen=1")
    changes.clear()

    await rig.hub.start()
    # No app was ever shown the mark of a closed session, so the count does not move and the
    # phones are told nothing.
    assert changes == []
    assert await rig.index.count_unseen([rig.device_id]) == 1
    closed = await rig.index.get(SESSION_ID)
    assert closed is not None
    assert closed.marked is False
    # Nothing holds the third session, which is no explicit close: it keeps what it had.
    third = await rig.index.get(THIRD_ID)
    assert third is not None
    assert third.marked is True
    await rig.hub.stop()


async def test_a_closed_session_that_comes_back_does_not_bring_its_mark_back(
    tmp_path: Path,
) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    await _publish(rig, state="stopped", control="none", archived=True)
    frames_of(rig.app)
    changes.clear()

    # A15: written to or attached again, and idle at the moment it says so.
    await _publish(rig, state="readonly", control="terminal")
    await _publish(rig, state="idle", control="remote")
    assert _marks(rig.app) == [False, False]
    assert changes == []
    assert await rig.index.count_unseen([rig.device_id]) == 0
    await rig.hub.stop()


async def test_the_mark_goes_with_the_session(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    await _publish(rig, OTHER_ID, state="idle")
    frames_of(rig.app)
    await rig.hub.handle_device_frame(
        rig.device, {"type": "session.removed", "session_id": OTHER_ID}
    )
    assert len(changes) == 1
    await rig.hub.handle_device_frame(
        rig.device, {"type": "session.removed", "session_id": SESSION_ID}
    )
    assert [frame["type"] for frame in frames_of(rig.app)] == ["session.removed"] * 2
    assert changes[-1] == BadgeChange(owner="admin", device_id=rig.device_id, session_id=SESSION_ID)
    assert len(changes) == 2
    await rig.hub.stop()


async def test_the_marks_go_with_a_deleted_device(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _rig(tmp_path, changes)
    await _mark(rig)
    await _mark(rig, OTHER_ID)
    frames_of(rig.app)
    changes.clear()

    await rig.hub.forget_device_sessions(rig.device_id, "admin")
    assert [frame["type"] for frame in frames_of(rig.app)] == ["session.removed"] * 2
    assert [change.owner for change in changes] == ["admin"]
    assert await rig.index.count_unseen([rig.device_id]) == 0
    await rig.hub.stop()


async def test_the_subscribe_reply_and_the_app_hello_carry_the_mark(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, [])
    await _mark(rig)
    frames_of(rig.app)
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.subscribe", "id": SEEN_ID, "session_id": SESSION_ID}
    )
    reply = frames_of(rig.app)[0]
    assert reply["result"]["session"]["unseen"] is True
    await rig.hub.stop()


# ---- the hub: a close on its way to the device ----


def _close(session_id: str = SESSION_ID, archived: bool = True) -> dict[str, Any]:
    return {
        "type": "session.archive",
        "id": CLOSE_ID,
        "session_id": session_id,
        "archived": archived,
    }


async def _answer_close(rig: HubRig, request: dict[str, Any], **reply: Any) -> None:
    await rig.hub.handle_device_frame(
        rig.device, {"type": "reply", "id": CLOSE_ID, "from": request["from"], **reply}
    )


class _Hearing:
    """The transitions the hub handed the push trigger, which is what a notification follows."""

    def __init__(self) -> None:
        self.transitions: list[SessionTransition] = []

    async def __call__(self, transition: SessionTransition) -> None:
        self.transitions.append(transition)


async def _closing_rig(
    tmp_path: Path, changes: list[BadgeChange], hearing: _Hearing, **options: Any
) -> HubRig:
    return await hub_rig(
        tmp_path, on_badge_change=changes.append, on_session_transition=hearing, **options
    )


async def test_closing_a_working_session_never_marks_it_or_cues_a_notification(
    tmp_path: Path,
) -> None:
    """The interrupt ends the turn, and the device says so before it says the session is closed."""
    changes: list[BadgeChange] = []
    hearing = _Hearing()
    rig = await _closing_rig(tmp_path, changes, hearing)
    await _publish(rig, state="running")
    frames_of(rig.app)
    await rig.hub.handle_app_frame(rig.app, _close())
    request = frames_of(rig.device)[-1]
    assert request["type"] == "session.archive"

    await _publish(rig, state="idle")
    closed: dict[str, Any] = {"state": "stopped", "control": "none", "archived": True}
    await _publish(rig, **closed)
    final = session_summary(SESSION_ID, rig.device_id, **closed)
    await _answer_close(rig, request, ok=True, result={"session": final})

    assert _marks(rig.app) == [False, False]
    assert changes == []
    assert await rig.index.count_unseen([rig.device_id]) == 0
    await rig.hub.stop()
    assert hearing.transitions == []


async def test_closing_a_marked_session_takes_its_mark_off_at_once(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    rig = await _closing_rig(tmp_path, changes, _Hearing())
    await _mark(rig)
    frames_of(rig.app)
    await rig.hub.handle_app_frame(rig.app, _close())

    # The held messages go with the close, and the queue is published before the session is.
    await _publish(rig, state="idle", queued=0)
    assert _marks(rig.app) == [False]
    assert len(changes) == 2
    assert await rig.index.count_unseen([rig.device_id]) == 0
    await rig.hub.stop()


async def test_a_close_the_device_refuses_ends_the_window(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    hearing = _Hearing()
    rig = await _closing_rig(tmp_path, changes, hearing)
    await _publish(rig, state="running")
    await rig.hub.handle_app_frame(rig.app, _close())
    request = frames_of(rig.device)[-1]
    await _answer_close(
        rig, request, ok=False, error={"code": "conflict", "message": "the session is busy"}
    )
    frames_of(rig.app)

    await _publish(rig, state="idle")
    assert _marks(rig.app) == [True]
    assert len(changes) == 1
    await rig.hub.stop()
    assert [item.state for item in hearing.transitions] == ["idle"]


async def test_a_close_the_device_never_answers_stops_holding_the_mark_back(
    tmp_path: Path,
) -> None:
    changes: list[BadgeChange] = []
    rig = await _closing_rig(tmp_path, changes, _Hearing(), request_timeout=0.05)
    await _publish(rig, state="running")
    await rig.hub.handle_app_frame(rig.app, _close())
    await asyncio.sleep(0.2)
    frames_of(rig.app)

    await _publish(rig, state="idle")
    assert _marks(rig.app) == [True]
    await rig.hub.stop()


async def test_a_close_outlives_the_socket_that_asked_for_it(tmp_path: Path) -> None:
    """A phone that taps Close and drops at once is not told that a turn finished."""
    changes: list[BadgeChange] = []
    hearing = _Hearing()
    rig = await _closing_rig(tmp_path, changes, hearing)
    await _publish(rig, state="running")
    await rig.hub.handle_app_frame(rig.app, _close())
    request = frames_of(rig.device)[-1]
    await rig.hub.detach_app(rig.app)

    # The interrupted turn ends and the close lands, with nobody left to hear either.
    await _publish(rig, state="idle")
    closed: dict[str, Any] = {"state": "stopped", "control": "none", "archived": True}
    await _publish(rig, **closed)
    final = session_summary(SESSION_ID, rig.device_id, **closed)
    await _answer_close(rig, request, ok=True, result={"session": final})
    assert changes == []
    assert await rig.index.count_unseen([rig.device_id]) == 0
    assert hearing.transitions == []

    # The device's answer ended the close though nobody read it: the session coming back to life
    # is news again.
    await _publish(rig, state="running")
    await _publish(rig, state="idle")
    assert len(changes) == 1
    await rig.hub.stop()
    assert [item.state for item in hearing.transitions] == ["running", "idle"]


async def test_a_close_ends_when_the_device_goes_offline(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    hearing = _Hearing()
    rig = await _closing_rig(tmp_path, changes, hearing, offline_grace=0.05)
    await _publish(rig, state="running")
    await rig.hub.handle_app_frame(rig.app, _close())
    frames_of(rig.app)

    # The link drops and nothing comes back inside the grace period: the request fails with the
    # device, and the close it was waiting on ends with it.
    await rig.hub.detach_device(rig.device)
    await asyncio.sleep(0.3)
    failed = [frame for frame in frames_of(rig.app) if frame.get("type") == "reply"]
    assert [frame["error"]["code"] for frame in failed] == ["device_offline"]

    await _publish(rig, state="idle")
    assert len(changes) == 1
    await rig.hub.stop()
    assert [item.state for item in hearing.transitions] == ["idle"]


async def test_a_close_nobody_answers_ends_with_its_own_timeout(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    hearing = _Hearing()
    rig = await _closing_rig(tmp_path, changes, hearing, request_timeout=0.05)
    await _publish(rig, state="running")
    await rig.hub.handle_app_frame(rig.app, _close())
    await rig.hub.detach_app(rig.app)
    await asyncio.sleep(0.2)

    await _publish(rig, state="idle")
    assert len(changes) == 1
    await rig.hub.stop()
    assert [item.state for item in hearing.transitions] == ["idle"]


async def test_the_window_is_one_sessions_and_only_for_a_close(tmp_path: Path) -> None:
    changes: list[BadgeChange] = []
    hearing = _Hearing()
    rig = await _closing_rig(tmp_path, changes, hearing)
    await _publish(rig, SESSION_ID, state="running")
    await _publish(rig, OTHER_ID, state="running")
    await rig.hub.handle_app_frame(rig.app, _close(SESSION_ID))
    frames_of(rig.app)

    await _publish(rig, OTHER_ID, state="idle")
    assert _marks(rig.app) == [True]
    await rig.hub.stop()
    assert [item.session["session_id"] for item in hearing.transitions] == [OTHER_ID]

    # Archiving to file a session away, rather than to close it, says nothing about its turn.
    rig = await _closing_rig(tmp_path / "again", changes, _Hearing())
    await _publish(rig, state="running")
    await rig.hub.handle_app_frame(rig.app, _close(archived=False))
    frames_of(rig.app)
    await _publish(rig, state="idle")
    assert _marks(rig.app) == [True]
    await rig.hub.stop()


# ---- the hub: a session a device's reply carries ----


async def _reply_to(
    rig: HubRig, request: dict[str, Any], session: dict[str, Any]
) -> dict[str, Any]:
    """Forward an app request, answer it as the device would, and return what the app gets."""
    await rig.hub.handle_app_frame(rig.app, request)
    forwarded = frames_of(rig.device)[-1]
    await rig.hub.handle_device_frame(
        rig.device,
        {
            "type": "reply",
            "id": forwarded["id"],
            "from": forwarded["from"],
            "ok": True,
            "result": {"session": session},
        },
    )
    replies = [frame for frame in frames_of(rig.app) if frame.get("type") == "reply"]
    return replies[-1]


async def test_a_session_in_a_device_reply_carries_the_gateways_mark(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, [])
    await _mark(rig)
    rig.device.drain()
    frames_of(rig.app)
    takeover = {"type": "session.takeover", "id": SEEN_ID, "session_id": SESSION_ID}
    summary = session_summary(SESSION_ID, rig.device_id, state="idle")
    reply = await _reply_to(rig, takeover, summary)
    assert reply["result"]["session"]["unseen"] is True

    # The close the reply reports ends the mark, as the publish behind it will.
    close = {"type": "session.archive", "id": OTHER_ID, "session_id": SESSION_ID, "archived": True}
    closed = session_summary(
        SESSION_ID, rig.device_id, state="stopped", control="none", archived=True
    )
    reply = await _reply_to(rig, close, closed)
    assert "unseen" not in reply["result"]["session"]
    await rig.hub.stop()


async def test_a_reply_shows_the_kept_mark_as_the_session_it_carries_says(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, [])
    await _mark(rig)
    await _publish(rig, state="idle", control="none")
    rig.device.drain()
    frames_of(rig.app)

    # Nothing held the session when the gateway last heard; the device has taken it over since.
    takeover = {"type": "session.takeover", "id": SEEN_ID, "session_id": SESSION_ID}
    held = session_summary(SESSION_ID, rig.device_id, state="idle", control="remote")
    reply = await _reply_to(rig, takeover, held)
    assert reply["result"]["session"]["unseen"] is True

    # And a session still nothing holds is shown none.
    still_unheld = session_summary(SESSION_ID, rig.device_id, state="idle", control="none")
    request = {"type": "session.set", "id": OTHER_ID, "session_id": SESSION_ID, "title": "x"}
    reply = await _reply_to(rig, request, still_unheld)
    assert "unseen" not in reply["result"]["session"]
    await rig.hub.stop()


async def test_a_device_cannot_mark_the_session_in_its_reply(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, [])
    await _publish(rig, state="idle")
    rig.device.drain()
    frames_of(rig.app)
    request = {"type": "session.set", "id": SEEN_ID, "session_id": SESSION_ID, "title": "x"}
    claimed = session_summary(SESSION_ID, rig.device_id, state="idle", unseen=True)
    reply = await _reply_to(rig, request, claimed)
    assert "unseen" not in reply["result"]["session"]
    await rig.hub.stop()


# ---- end to end ----


def _device_headers(enrolled: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {enrolled['device_token']}"}


def test_every_list_an_app_reads_carries_the_mark(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    running = session_summary(SESSION_ID, enrolled["device_id"], state="running")
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(sessions=[running]))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            idle = session_summary(SESSION_ID, enrolled["device_id"], state="idle")
            device.send_json({"type": "session.updated", "session": idle})
            assert drain_until(app, "session.updated")["session"]["unseen"] is True
        with client.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
    assert [item.get("unseen") for item in hello["sessions"]] == [True]
    listed = client.get("/api/sessions", headers=auth).json()["sessions"]
    assert [item.get("unseen") for item in listed] == [True]


def test_the_mark_survives_a_gateway_restart(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with TestClient(create_app(build_state(config))) as first:
        auth = {"Authorization": f"Bearer {sign_in(first, 'admin', config.password)}"}
        enrolled = enroll_device(first, auth)
        with first.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
            running = session_summary(SESSION_ID, enrolled["device_id"], state="running")
            device.send_json(device_hello(sessions=[running]))
            device.receive_json()
            with first.websocket_connect("/ws/app", headers=auth) as app:
                drain_until(app, "hello")
                idle = session_summary(SESSION_ID, enrolled["device_id"], state="idle")
                device.send_json({"type": "session.updated", "session": idle})
                drain_until(app, "session.updated")

    with TestClient(create_app(build_state(config))) as second:
        auth = {"Authorization": f"Bearer {sign_in(second, 'admin', config.password)}"}
        with second.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
            assert [item.get("unseen") for item in hello["sessions"]] == [True]
            app.send_json(_seen())
            assert drain_until(app, "reply")["ok"] is True
            assert "unseen" not in drain_until(app, "session.updated")["session"]


def test_a_revoked_device_takes_its_marks_with_it(client: TestClient, auth: dict[str, str]) -> None:
    """The account hears the sessions go even though the device's record is already gone."""
    enrolled = enroll_device(client, auth)
    running = session_summary(SESSION_ID, enrolled["device_id"], state="running")
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(sessions=[running]))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            idle = session_summary(SESSION_ID, enrolled["device_id"], state="idle")
            device.send_json({"type": "session.updated", "session": idle})
            assert drain_until(app, "session.updated")["session"]["unseen"] is True
            deleted = client.delete(f"/api/devices/{enrolled['device_id']}", headers=auth)
            assert deleted.status_code == 200
            removed = drain_until(app, "session.removed")
            assert removed["session_id"] == SESSION_ID
    assert client.get("/api/sessions", headers=auth).json()["sessions"] == []
