"""Amendment A11: which of the daemon's threads are sessions, and when one is forgotten.

`thread/list` is one page of the whole machine's history, so a session missing
from it was either deleted in Codex or cut off by the page size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex.daemon import service, threads
from rc_client.models import now_ms
from tests.fake_codex_daemon import Responder
from tests.test_codex_daemon_sessions import THREAD, Harness, started, thread_row
from tests.test_codex_daemon_sessions import harness as harness

BASE = 1788946200


def row(thread_id: str, created: int, used: int) -> dict[str, Any]:
    """A history row created `created` seconds after BASE and last used `used` seconds after it."""
    return thread_row(thread_id, createdAt=BASE + created, updatedAt=BASE + used)


def paging(rows: list[dict[str, Any]]) -> Responder:
    """A `thread/list` that pages the way Codex does, from the rows as they are now.

    The page is ordered by the key asked for, newest first, and cut at `limit`,
    with a cursor when there is more behind it and none when there is not.
    Without a `sortKey` Codex orders by creation, which is not what the device wants.
    """

    def answer(method: str, params: dict[str, Any]) -> Any:
        if method != "thread/list":
            return None
        key = {"created_at": "createdAt", "updated_at": "updatedAt"}[
            params.get("sortKey", "created_at")
        ]
        limit = params.get("limit", 25)
        ordered = sorted(rows, key=lambda each: each[key], reverse=True)
        return {"data": ordered[:limit], "nextCursor": "more" if len(ordered) > limit else None}

    return answer


def removed(harness: Harness) -> list[str]:
    return [f["session_id"] for f in harness.frames if f.get("type") == "session.removed"]


# ------------------------------------------------------------------- the page


async def test_the_history_is_asked_for_most_recently_used_first(harness: Harness) -> None:
    await started(harness, loaded=[])
    assert harness.daemon.sent("thread/list")[-1] == {
        "limit": service.HISTORY_LIMIT,
        "sortKey": "updated_at",
        "sortDirection": "desc",
    }


async def test_a_thread_started_long_ago_and_used_lately_is_kept(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordered by creation, the page would have dropped it, and the device with it."""
    monkeypatch.setattr(service, "HISTORY_LIMIT", 3)
    rows = [
        row("t-old", created=0, used=500),
        row("t-a", created=200, used=210),
        row("t-b", created=300, used=310),
    ]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])
    assert set(harness.hub.entries) == {"t-old", "t-a", "t-b"}

    # The old thread is in use again, and the apps have heard of it; a newer
    # thread has arrived. By creation the page is now t-c, t-b and t-a, and
    # every one of them was used before t-old was.
    await harness.hub.entry("t-old").channel.publish_summary()
    rows[0]["updatedAt"] = BASE + 900
    rows.append(row("t-c", created=400, used=410))
    await harness.service.refresh()

    assert "t-old" in harness.hub.entries
    # t-a is what the page now cuts off: used before everything on it.
    assert "t-a" in harness.hub.entries
    assert removed(harness) == []


async def test_a_thread_tied_with_the_pages_oldest_may_only_have_been_cut_off(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex counts whole seconds, so two threads often share the last place on a page."""
    monkeypatch.setattr(service, "HISTORY_LIMIT", 2)
    rows = [row("t-q", created=10, used=200), row("t-t", created=20, used=200)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])

    rows.insert(0, row("t-p", created=30, used=300))
    await harness.service.refresh()

    assert {"t-q", "t-t"} <= set(harness.hub.entries)
    assert removed(harness) == []


async def test_publishing_a_session_does_not_make_its_thread_look_newly_used(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's `updated_at` is when the apps last heard of it, not when Codex saw it used."""
    monkeypatch.setattr(service, "HISTORY_LIMIT", 2)
    rows = [row("t-s", created=10, used=100), row("t-v", created=20, used=200)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])
    await harness.hub.entry("t-s").channel.publish_summary()

    rows.append(row("t-w", created=30, used=300))
    await harness.service.refresh()

    assert "t-s" in harness.hub.entries
    assert removed(harness) == []


async def test_a_thread_missing_from_the_whole_history_is_gone_however_old(
    harness: Harness,
) -> None:
    """A page with nothing behind it has no end to fall off, so no cutoff to hide behind."""
    rows = [row("t-new", created=20, used=300), row("t-old", created=10, used=100)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])
    assert set(harness.hub.entries) == {"t-new", "t-old"}

    rows.pop()  # t-old is deleted in Codex, and was used before everything that is left.
    await harness.service.refresh()

    assert set(harness.hub.entries) == {"t-new"}
    assert removed(harness) == ["t-old"]


async def test_a_loaded_thread_is_kept_even_when_the_whole_history_lacks_it(
    harness: Harness,
) -> None:
    """A thread being driven may have no rollout to list yet."""
    rows = [row("t-new", created=20, used=300), row("t-live", created=10, used=100)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=["t-live"])

    rows.pop()
    await harness.service.refresh()

    assert set(harness.hub.entries) == {"t-new", "t-live"}
    assert removed(harness) == []


async def test_an_empty_page_forgets_nothing(harness: Harness) -> None:
    """A daemon that lists nothing is no evidence that its threads are gone."""
    rows = [row("t-a", created=10, used=100), row("t-b", created=20, used=200)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])

    rows.clear()
    await harness.service.refresh()

    assert set(harness.hub.entries) == {"t-a", "t-b"}
    assert removed(harness) == []


async def test_a_listing_that_failed_forgets_nothing(harness: Harness) -> None:
    rows = [row("t-a", created=10, used=100), row("t-b", created=20, used=200)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])

    harness.daemon.errors["thread/list"] = "the state database is busy"
    await harness.service.refresh()

    assert set(harness.hub.entries) == {"t-a", "t-b"}
    assert removed(harness) == []


async def test_a_failed_list_of_loaded_threads_forgets_nothing_that_refresh(
    harness: Harness,
) -> None:
    """Without it nothing can be called gone, however whole the history looks."""
    rows = [row("t-new", created=20, used=300), row("t-old", created=10, used=100)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])
    rows.pop()  # t-old is deleted in Codex.

    harness.daemon.errors["thread/loaded/list"] = "the daemon is busy"
    await harness.service.refresh()
    assert set(harness.hub.entries) == {"t-new", "t-old"}
    assert removed(harness) == []

    # The next refresh that hears from the daemon in full forgets it as usual.
    del harness.daemon.errors["thread/loaded/list"]
    await harness.service.refresh()
    assert set(harness.hub.entries) == {"t-new"}
    assert removed(harness) == ["t-old"]


async def test_a_thread_the_phone_just_created_survives_a_failed_list_of_loaded_threads(
    harness: Harness, tmp_path: Path
) -> None:
    """It has no rollout to list yet, so the loaded list is all that says it exists."""
    rows = [row("t-a", created=10, used=100)]
    harness.daemon.responder = paging(rows)
    await started(harness, loaded=[])
    harness.daemon.replies["thread/start"] = {"thread": {"id": "t-made"}}
    created = await harness.hub.create({"agent": "codex", "cwd": str(tmp_path)})
    assert created["session"]["session_id"] == "t-made"

    harness.daemon.errors["thread/loaded/list"] = "the daemon is busy"
    await harness.service.refresh()
    assert "t-made" in harness.hub.entries
    assert "t-made" not in removed(harness)

    # Once the list is back it names the thread, which is what keeps it.
    del harness.daemon.errors["thread/loaded/list"]
    harness.daemon.replies["thread/loaded/list"] = {"data": ["t-made"]}
    await harness.service.refresh()
    assert "t-made" in harness.hub.entries
    assert "t-made" not in removed(harness)


@pytest.mark.parametrize(
    ("listing", "whole"),
    [
        ({"data": [{"id": "t"}], "nextCursor": None}, True),
        ({"data": [{"id": "t"}]}, True),
        ({"data": [{"id": "t"}], "nextCursor": "older"}, False),
        ({"data": [], "nextCursor": None}, True),
        ({}, False),
    ],
)
def test_a_listing_is_the_whole_history_when_it_ends_with_no_cursor(
    listing: dict[str, Any], whole: bool
) -> None:
    """A listing that failed is `{}`, which is no page at all."""
    assert service._whole_history(listing) is whole


# -------------------------------------------------------------------- the clock


def test_codex_counts_seconds_and_the_protocol_counts_milliseconds() -> None:
    summary = threads.ThreadSummary.parse(thread_row())
    assert summary is not None
    assert (summary.created_at, summary.updated_at) == (1788946200_000, 1788946241_000)


def test_a_thread_with_no_timestamps_is_as_old_as_this_moment() -> None:
    before = now_ms()
    summary = threads.ThreadSummary.parse(thread_row(createdAt=None, updatedAt=None))
    after = now_ms()
    assert summary is not None
    assert before <= summary.created_at <= after
    assert summary.updated_at == summary.created_at


async def test_an_adopted_session_carries_its_times_in_milliseconds(harness: Harness) -> None:
    await started(harness, loaded=[])
    session = harness.hub.entry(THREAD).session
    assert (session.created_at, session.updated_at) == (1788946200_000, 1788946241_000)
