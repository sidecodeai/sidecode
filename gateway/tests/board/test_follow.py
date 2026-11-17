"""A62: which boards the gateway keeps current — opened in an app within seven days, board on."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rc_gateway.board.follow import Follow, FollowedSet
from rc_gateway.board.limits import DAY_SECONDS, FOLLOW_DAYS, UNVIEWED_DAYS
from rc_gateway.board.policy import OperatorPolicy
from rc_gateway.board.service import BoardService
from rc_gateway.board.store import BoardRow, BoardStore
from rc_gateway.preference_store import Preferences

from .fakes import DEVICE, OWNER, PATIENCE, SESSION, START, Rig, make_rig, rounds, settle, until

DAY = DAY_SECONDS


async def until_gone(rig: Rig) -> None:
    deadline = asyncio.get_running_loop().time() + PATIENCE
    while await rig.store.get(SESSION) is not None:
        assert asyncio.get_running_loop().time() < deadline, "the board stayed"
        await asyncio.sleep(0.01)


async def opened(rig: Rig) -> None:
    """An app opened the conversation while nothing is new: the board is followed, not read."""
    connection = rig.app()
    reply = await rig.get(connection)
    assert reply["result"]["status"] == {"state": "current", "reason": None}
    rig.service.on_app_detached(connection)


async def turn_end_reads(rig: Rig, session_id: str = SESSION, *, expect: int) -> int:
    """How many history reads one turn end of the session causes, ``expect`` waited for."""
    before = len(rig.host.history_reads())
    rig.host.history[session_id] = rounds(2)
    rig.turn_ends(session_id, last_seq=6)
    await rig.advance(20)
    await until(lambda: len(rig.host.history_reads()) - before >= expect)
    await settle()
    return len(rig.host.history_reads()) - before


async def test_opening_with_the_board_on_creates_the_row_and_follows(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await opened(rig)
    row = await rig.store.get(SESSION)
    assert row is not None
    assert (row.revision, row.username, row.device_id, row.viewed_at) == (
        0,
        OWNER,
        DEVICE,
        int(START * 1000),
    )
    assert await turn_end_reads(rig, expect=1) == 1


async def test_with_the_board_off_an_open_records_nothing(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, board_on=False)
    connection = rig.app()
    reply = await rig.get(connection)
    assert reply["result"] == {"board": None, "status": {"state": "off", "reason": None}}
    assert await rig.store.get(SESSION) is None
    assert await turn_end_reads(rig, expect=0) == 0


async def test_a_follow_lapses_after_seven_days_and_the_board_stays_until_thirty(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    await opened(rig)
    rig.clock.now += FOLLOW_DAYS * DAY + 1
    assert await turn_end_reads(rig, expect=0) == 0
    assert await rig.store.get(SESSION) is not None
    rig.clock.now += (UNVIEWED_DAYS - FOLLOW_DAYS) * DAY
    await rig.service.sweep()
    assert await rig.store.get(SESSION) is None


async def test_a_restart_follows_exactly_the_boards_opened_within_seven_days(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    now = int(START * 1000)
    fresh, old = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    for session_id, age in ((fresh, 6 * DAY), (old, 8 * DAY)):
        rig.host.own(session_id)
        rig.sessions.put(session_id)
        await rig.store.create(session_id, DEVICE, OWNER, now - age * 1000)
    await rig.service.start()
    try:
        assert await turn_end_reads(rig, fresh, expect=1) == 1
        assert await turn_end_reads(rig, old, expect=0) == 0
    finally:
        await rig.service.stop()


async def test_a_dropped_session_takes_its_board_and_its_follow(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await opened(rig)
    rig.service.on_session_dropped(SESSION)
    await until_gone(rig)
    assert await turn_end_reads(rig, expect=0) == 0


@pytest.mark.parametrize("deleted", ["device", "account", "board off"])
async def test_a_revoked_device_a_deleted_account_and_the_board_turned_off_end_following(
    tmp_path: Path, deleted: str
) -> None:
    rig = await make_rig(tmp_path)
    await opened(rig)
    if deleted == "device":
        await rig.service.forget_device(DEVICE)
    elif deleted == "account":
        await rig.service.forget_account(OWNER)
    else:
        await rig.service.on_preferences(OWNER, rig.preferences.turn(False))
        rig.preferences.turn(True)
    assert await rig.store.get(SESSION) is None
    assert await turn_end_reads(rig, expect=0) == 0


class CountingStore(BoardStore):
    """The real store, counting every read and write it is asked for."""

    calls = 0

    async def get(self, session_id: str) -> BoardRow | None:
        self.calls += 1
        return await super().get(session_id)

    async def create(self, session_id: str, device_id: str, username: str, viewed_at: int) -> None:
        self.calls += 1
        await super().create(session_id, device_id, username, viewed_at)

    async def viewed(self, session_id: str, viewed_at: int) -> None:
        self.calls += 1
        await super().viewed(session_id, viewed_at)

    async def write_board(self, row: BoardRow) -> BoardRow | None:
        self.calls += 1
        return await super().write_board(row)

    async def write_state(self, row: BoardRow) -> bool:
        self.calls += 1
        return await super().write_state(row)

    async def remove(self, session_id: str) -> None:
        self.calls += 1
        await super().remove(session_id)


async def test_the_hooks_touch_no_disk(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    store = CountingStore(tmp_path / "counted.sqlite3")
    service = BoardService(
        rig.host,
        rig.sessions,
        store,
        rig.preferences,
        OperatorPolicy(rig.model),
        clock=lambda: rig.clock.now,
        sleep=rig.clock.sleep,
    )
    connection = rig.app()
    await service.handle_app_frame(
        connection,
        {"type": "board.get", "id": "61e50c72-d2bc-4a64-be32-62fdd1ba4635", "session_id": SESSION},
    )
    store.calls = 0
    previous = rig.sessions.put(state="running", last_seq=6)
    indexed = rig.sessions.put(state="idle", last_seq=6)
    # The four hooks a device's read loop calls, and the one an app socket's close calls.
    service.on_session_change(previous, indexed, OWNER)
    service.on_session_change(indexed, rig.sessions.put(state="running", last_seq=7), OWNER)
    service.on_device_online(DEVICE)
    service.on_app_detached(connection)
    service.on_session_dropped(SESSION)
    assert store.calls == 0
    await service.stop()


def test_the_followed_set_is_indexed_by_device_and_account() -> None:
    followed = FollowedSet()
    day_ms = DAY * 1000
    followed.load(
        [
            Follow("s1", "d1", "ada", 10 * day_ms),
            Follow("s2", "d1", "ada", 10 * day_ms),
            Follow("s3", "d2", "grace", 10 * day_ms),
            Follow("s4", "d2", "grace", 1 * day_ms),
        ],
        now_ms=10 * day_ms,
    )
    assert len(followed) == 3
    assert [f.session_id for f in followed.of_device("d1", 10 * day_ms)] in (
        ["s1", "s2"],
        ["s2", "s1"],
    )
    followed.view(Follow("s1", "d1", "ada", 9 * day_ms))
    assert followed.get("s1", 10 * day_ms) == Follow("s1", "d1", "ada", 10 * day_ms)
    assert sorted(followed.drop_user("ada")) == ["s1", "s2"]
    followed.drop_device("d2")
    assert len(followed) == 0
    assert Preferences(task_board=True).board_on and not Preferences().board_on
