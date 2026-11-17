"""How a model's answer changes a board: fixed rules, the same every time (A62).

The model proposes; these rules decide. An item the answer names takes its status and text; an
item it leaves out stays exactly as it was, so an open request is never lost because the model
forgot it; `dropped` removes one. New items get the board's next ids, which are never given twice,
and are added only while the board has room for open items. Every open item is kept, and of the
done ones only the most recently done.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .limits import DONE_KEPT, OPEN_MAX

if TYPE_CHECKING:
    from .prompt import Answer

DOING = "doing"
TODO = "todo"
DONE = "done"
ITEM_STATUSES = frozenset({DOING, TODO, DONE})


@dataclass(frozen=True)
class Item:
    """One request on a board, as stored: the wire's fields and when it was done."""

    id: str
    text: str
    status: str
    created_at: int
    updated_at: int
    #: Set when the item becomes done, cleared when it leaves done; stored, never sent.
    done_at: int | None = None

    @property
    def number(self) -> int:
        return int(self.id[1:]) if self.id[1:].isdigit() else 0

    def view(self) -> dict[str, Any]:
        """``objects.json#/$defs/BoardItem``."""
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def stored(self) -> dict[str, Any]:
        return {**self.view(), "done_at": self.done_at}


def item_from_stored(data: Mapping[str, Any]) -> Item | None:
    """An item read back from the store; ``None`` for anything the store should not hold."""
    try:
        item = Item(
            id=str(data["id"]),
            text=str(data["text"]),
            status=str(data["status"]),
            created_at=int(data["created_at"]),
            updated_at=int(data["updated_at"]),
            done_at=None if data.get("done_at") is None else int(data["done_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return item if item.status in ITEM_STATUSES else None


def merge(
    items: Sequence[Item], answer: Answer, *, next_item: int, now: int
) -> tuple[tuple[Item, ...], int]:
    """The board after ``answer``, and the counter for the next new item."""
    merged: list[Item] = []
    for item in items:
        change = answer.changes.get(item.id)
        if change is None:
            merged.append(item)
        elif change[0] in ITEM_STATUSES:
            merged.append(_changed(item, *change, now=now))
    open_count = sum(1 for item in merged if item.status != DONE)
    for status, text in answer.added:
        if status != DONE:
            if open_count >= OPEN_MAX:
                continue
            open_count += 1
        done_at = now if status == DONE else None
        merged.append(Item(f"t{next_item}", text, status, now, now, done_at))
        next_item += 1
    return prune(merged), next_item


def _changed(item: Item, status: str, text: str, *, now: int) -> Item:
    if status == item.status and text == item.text:
        return item
    if status != DONE:
        done_at = None
    elif item.status == DONE:
        done_at = item.done_at
    else:
        done_at = now
    return replace(item, status=status, text=text, updated_at=now, done_at=done_at)


def _done_order(item: Item) -> tuple[int, int]:
    return (item.done_at or 0, item.number)


def prune(items: Iterable[Item]) -> tuple[Item, ...]:
    """Every open item, and the `DONE_KEPT` most recently done, in the wire's order."""
    listed = list(items)
    done = sorted((item for item in listed if item.status == DONE), key=_done_order, reverse=True)
    return ordered([*(item for item in listed if item.status != DONE), *done[:DONE_KEPT]])


def ordered(items: Iterable[Item]) -> tuple[Item, ...]:
    """Doing, then to do, each in the order they were added; then done, the latest first."""
    listed = list(items)

    def added(item: Item) -> tuple[int, int]:
        return (item.created_at, item.number)

    doing = sorted((item for item in listed if item.status == DOING), key=added)
    todo = sorted((item for item in listed if item.status == TODO), key=added)
    done = sorted((item for item in listed if item.status == DONE), key=_done_order, reverse=True)
    return (*doing, *todo, *done)
