"""A62: the boards on the disk — one row per session, and every way a row leaves."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from rc_gateway.board.merge import Item
from rc_gateway.board.store import BoardRow, BoardStore


def store(tmp_path: Path) -> BoardStore:
    return BoardStore(tmp_path / "boards.sqlite3")


def test_the_file_is_not_world_readable(tmp_path: Path) -> None:
    store(tmp_path)
    assert (tmp_path / "boards.sqlite3").stat().st_mode & 0o077 == 0


async def test_a_row_is_created_empty_once_and_read_back(tmp_path: Path) -> None:
    boards = store(tmp_path)
    assert await boards.get("s1") is None
    await boards.create("s1", "d1", "ada", 1000)
    await boards.create("s1", "d1", "ada", 9000)
    assert await boards.get("s1") == BoardRow("s1", "d1", "ada", viewed_at=1000)


async def test_a_view_only_moves_forward(tmp_path: Path) -> None:
    boards = store(tmp_path)
    await boards.create("s1", "d1", "ada", 1000)
    await boards.viewed("s1", 5000)
    await boards.viewed("s1", 3000)
    row = await boards.get("s1")
    assert row is not None and row.viewed_at == 5000


async def test_a_board_is_saved_whole_and_read_back(tmp_path: Path) -> None:
    boards = store(tmp_path)
    await boards.create("s1", "d1", "ada", 1000)
    items = (
        Item("t2", "Run the tests", "doing", 10, 20),
        Item("t1", "Fix the bug", "done", 5, 30, done_at=30),
    )
    board = BoardRow(
        "s1",
        "d1",
        "ada",
        items=items,
        next_item=3,
        through_seq=41,
        checked_seq=43,
        queue_hash="abc",
        partial=True,
        updated_at=2000,
        viewed_at=1000,
        failures=1,
        retry_at=9000,
    )
    saved = await boards.write_board(board)
    assert saved == replace(board, revision=1)
    assert await boards.get("s1") == saved
    # The view is the store's own column: a write never moves it.
    assert await boards.write_board(replace(board, viewed_at=1)) is not None
    row = await boards.get("s1")
    assert row is not None and row.viewed_at == 1000


async def test_the_cursors_and_the_back_off_are_written_without_the_board(tmp_path: Path) -> None:
    boards = store(tmp_path)
    await boards.create("s1", "d1", "ada", 1000)
    saved = await boards.write_board(
        BoardRow("s1", "d1", "ada", items=(Item("t1", "Fix it", "todo", 1, 1),), next_item=2)
    )
    assert saved is not None
    state = replace(
        saved,
        items=(),
        next_item=9,
        through_seq=5,
        checked_seq=7,
        queue_hash="q",
        failures=2,
        retry_at=4000,
    )
    assert await boards.write_state(state) is True
    row = await boards.get("s1")
    assert row is not None
    assert (row.revision, row.items, row.next_item) == (saved.revision, saved.items, 2)
    assert (row.through_seq, row.checked_seq, row.queue_hash, row.failures, row.retry_at) == (
        5,
        7,
        "q",
        2,
        4000,
    )


async def test_a_write_changes_nothing_once_the_row_is_gone_or_another_accounts(
    tmp_path: Path,
) -> None:
    boards = store(tmp_path)
    assert await boards.write_board(BoardRow("gone", "d1", "ada")) is None
    assert await boards.write_state(BoardRow("gone", "d1", "ada")) is False
    await boards.create("s1", "d1", "ada", 1000)
    assert await boards.write_board(BoardRow("s1", "d1", "grace")) is None
    row = await boards.get("s1")
    assert row is not None and row.revision == 0
    # The refused writes did not move the counter.
    written = await boards.write_board(BoardRow("s1", "d1", "ada"))
    assert written is not None and written.revision == 1


async def test_one_counter_numbers_every_board_and_never_goes_back(tmp_path: Path) -> None:
    boards = store(tmp_path)
    for session in ("s1", "s2"):
        await boards.create(session, "d1", "ada", 1000)
    revisions = []
    for session in ("s1", "s2", "s1"):
        written = await boards.write_board(BoardRow(session, "d1", "ada"))
        assert written is not None
        revisions.append(written.revision)
    assert revisions == [1, 2, 3]
    # Deleted and made again, the board goes on from where the store is.
    await boards.remove_for_user("ada")
    await boards.create("s1", "d1", "ada", 2000)
    again = await store(tmp_path).write_board(BoardRow("s1", "d1", "ada"))
    assert again is not None and again.revision == 4


async def test_rows_leave_by_session_device_and_account(tmp_path: Path) -> None:
    boards = store(tmp_path)
    for session, device, user in (("s1", "d1", "ada"), ("s2", "d1", "ada"), ("s3", "d2", "ada")):
        await boards.create(session, device, user, 1000)
    await boards.create("s4", "d3", "grace", 1000)
    await boards.remove("s1")
    assert await boards.get("s1") is None and await boards.get("s2") is not None
    await boards.remove_for_device("d1")
    assert await boards.get("s2") is None and await boards.get("s3") is not None
    await boards.remove_for_user("ada")
    assert await boards.get("s3") is None
    assert await boards.get("s4") is not None


async def test_every_board_is_deleted_at_once_and_the_counter_goes_on(tmp_path: Path) -> None:
    """What a new board provider does (A65): no board is left, and none goes back in number."""
    boards = store(tmp_path)
    for session, user in (("s1", "ada"), ("s2", "grace")):
        await boards.create(session, "d1", user, 1000)
        assert await boards.write_board(BoardRow(session, "d1", user)) is not None
    assert await boards.clear() == 2
    assert await boards.get("s1") is None and await boards.get("s2") is None
    assert await boards.clear() == 0
    await boards.create("s1", "d1", "ada", 2000)
    again = await boards.write_board(BoardRow("s1", "d1", "ada"))
    assert again is not None and again.revision == 3


async def test_the_sweep_deletes_what_nobody_opened_since_the_cutoff(tmp_path: Path) -> None:
    boards = store(tmp_path)
    await boards.create("old", "d1", "ada", 1000)
    await boards.create("watched", "d1", "ada", 1000)
    await boards.create("new", "d1", "ada", 5000)
    assert await boards.sweep(5000, keep={"watched"}) == ["old"]
    assert await boards.get("old") is None and await boards.get("new") is not None
    assert await boards.get("watched") is not None
    assert await boards.sweep(5000) == ["watched"]
    assert [row.session_id for row in await boards.followed(5000)] == ["new"]
    assert await boards.followed(5001) == []


async def test_opening_again_changes_nothing(tmp_path: Path) -> None:
    boards = store(tmp_path)
    await boards.create("s1", "d1", "ada", 1000)
    again = store(tmp_path)
    assert await again.get("s1") == BoardRow("s1", "d1", "ada", viewed_at=1000)
    with sqlite3.connect(tmp_path / "boards.sqlite3") as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(boards)")]
        counters = connection.execute("SELECT id, revision FROM board_meta").fetchall()
    assert counters == [(1, 0)]
    assert columns == [
        "session_id",
        "device_id",
        "username",
        "revision",
        "items",
        "next_item",
        "through_seq",
        "checked_seq",
        "queue_hash",
        "partial",
        "updated_at",
        "viewed_at",
        "failures",
        "retry_at",
    ]


async def test_an_unreadable_item_is_left_out(tmp_path: Path) -> None:
    boards = store(tmp_path)
    await boards.create("s1", "d1", "ada", 1000)
    with sqlite3.connect(tmp_path / "boards.sqlite3") as connection:
        connection.execute(
            "UPDATE boards SET items=? WHERE session_id='s1'",
            (
                '[{"id":"t1","text":"ok","status":"todo","created_at":1,"updated_at":1},'
                '{"id":"t2","text":"bad","status":"blocked","created_at":1,"updated_at":1},'
                '"nonsense"]',
            ),
        )
    row = await boards.get("s1")
    assert row is not None and [item.id for item in row.items] == ["t1"]
