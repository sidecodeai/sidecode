"""A62: how a model's answer changes a board, by fixed rules."""

from __future__ import annotations

from rc_gateway.board.limits import DONE_KEPT, OPEN_MAX
from rc_gateway.board.merge import Item, merge, ordered, prune
from rc_gateway.board.prompt import Answer
from rc_gateway.board.store import BoardRow

NOW = 5000


def item(
    number: int, status: str = "todo", *, created: int = 0, done_at: int | None = None
) -> Item:
    at = created or 1000 + number
    return Item(f"t{number}", f"task {number}", status, at, at, done_at)


def ids(items: tuple[Item, ...]) -> list[str]:
    return [entry.id for entry in items]


def test_an_open_item_the_model_left_out_is_kept_as_it_was() -> None:
    board = (item(1, "doing"), item(2))
    merged, next_item = merge(board, Answer({"t1": ("done", "task 1")}, ()), next_item=3, now=NOW)
    assert [(entry.id, entry.status) for entry in merged] == [("t2", "todo"), ("t1", "done")]
    assert merged[0] == board[1]
    assert next_item == 3


def test_dropped_removes_an_item() -> None:
    board = (item(1), item(2))
    merged, _ = merge(board, Answer({"t1": ("dropped", "task 1")}, ()), next_item=3, now=NOW)
    assert ids(merged) == ["t2"]


def test_a_change_moves_updated_at_and_done_at_follows_done() -> None:
    board = (item(1, "doing"), item(2, "done", done_at=1500), item(3))
    answer = Answer(
        {
            "t1": ("done", "task 1"),
            "t2": ("done", "task 2 reworded"),
            "t3": ("todo", "task 3"),
        },
        (),
    )
    merged, _ = merge(board, answer, next_item=4, now=NOW)
    by_id = {entry.id: entry for entry in merged}
    assert (by_id["t1"].done_at, by_id["t1"].updated_at) == (NOW, NOW)
    # Still done: its text changed, and it keeps the moment it was done.
    assert (by_id["t2"].done_at, by_id["t2"].updated_at) == (1500, NOW)
    # Unchanged: untouched.
    assert by_id["t3"] == board[2]
    reopened, _ = merge(merged, Answer({"t1": ("doing", "task 1")}, ()), next_item=4, now=NOW + 1)
    assert next(entry for entry in reopened if entry.id == "t1").done_at is None


def test_new_items_take_the_next_ids_in_the_answers_order() -> None:
    answer = Answer({}, (("todo", "first"), ("done", "second"), ("doing", "third")))
    merged, next_item = merge((), answer, next_item=7, now=NOW)
    assert {entry.id: entry.text for entry in merged} == {
        "t7": "first",
        "t8": "second",
        "t9": "third",
    }
    assert next_item == 10
    assert all(entry.created_at == entry.updated_at == NOW for entry in merged)
    assert next(entry for entry in merged if entry.id == "t8").done_at == NOW


def test_new_open_items_stop_at_the_open_limit_and_done_ones_still_come() -> None:
    board = tuple(item(n) for n in range(1, OPEN_MAX + 1))
    answer = Answer({}, (("todo", "one too many"), ("done", "finished on the spot")))
    merged, next_item = merge(board, answer, next_item=OPEN_MAX + 1, now=NOW)
    assert sum(1 for entry in merged if entry.status != "done") == OPEN_MAX
    assert [entry.text for entry in merged if entry.status == "done"] == ["finished on the spot"]
    assert next_item == OPEN_MAX + 2
    # The limit is on adding: an existing item reopened past it stays open.
    full = (*board, item(99, "done", done_at=1))
    reopened, _ = merge(full, Answer({"t99": ("todo", "task 99")}, ()), next_item=100, now=NOW)
    assert sum(1 for entry in reopened if entry.status != "done") == OPEN_MAX + 1


def test_only_the_most_recently_done_are_kept() -> None:
    done = [item(n, "done", done_at=2000 + n) for n in range(1, 8)]
    # Two done at the same moment: the higher id is the later one.
    done.append(item(8, "done", done_at=2007))
    kept = prune([item(20), *done])
    assert ids(kept) == ["t20", "t8", "t7", "t6", "t5", "t4"]
    assert len([entry for entry in kept if entry.status == "done"]) == DONE_KEPT


def test_the_wire_order_is_doing_then_to_do_then_done() -> None:
    items = [
        item(1, "done", done_at=3000),
        item(2, "todo", created=10),
        item(3, "doing", created=30),
        item(4, "doing", created=20),
        item(5, "done", done_at=4000),
        item(10, "todo", created=10),
    ]
    assert ids(ordered(items)) == ["t4", "t3", "t2", "t10", "t5", "t1"]


def test_ids_are_never_given_twice_not_even_after_starting_over() -> None:
    row = BoardRow("s", "d", "ada", revision=3, items=(item(1), item(2)), next_item=3)
    cleared = row.cleared()
    assert cleared.items == () and cleared.next_item == 3
    merged, next_item = merge(cleared.items, Answer({}, (("todo", "again"),)), next_item=3, now=NOW)
    assert ids(merged) == ["t3"] and next_item == 4


def test_an_answer_that_changes_nothing_leaves_the_items_equal() -> None:
    board = (item(1, "doing"), item(2))
    same = Answer({"t1": ("doing", "task 1")}, ())
    assert merge(board, same, next_item=3, now=NOW)[0] == ordered(board)
    assert merge(board, Answer({}, ()), next_item=3, now=NOW)[0] == ordered(board)
