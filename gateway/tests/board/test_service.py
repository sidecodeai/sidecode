"""A62: when boards are updated, what an update reads and stores, and who is told.

The board runs against fakes here: a device that answers history pages, a model that answers from
a script, and a clock the test moves. A followed board is updated at a turn's end whether or not
an app watches it; a session nobody opened in the last seven days is never read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from rc_gateway.board.limits import (
    BACKOFF_SECONDS,
    CONCURRENT_CALLS,
    HISTORY_PAGE_EVENTS,
    HISTORY_PAGES,
    MIN_INTERVAL_SECONDS,
    TURN_END_DEBOUNCE_SECONDS,
)
from rc_gateway.board.llm import ModelFailure
from rc_gateway.board.store import BoardRow

from .fakes import (
    DEVICE,
    OWNER,
    SESSION,
    Rig,
    answer,
    make_rig,
    person,
    reply,
    rounds,
    settle,
    stored,
    told,
    until,
)

ADD_FIX = answer((None, "todo", "Fix the login bug"))


async def row_of(rig: Rig, session_id: str = SESSION) -> BoardRow:
    row = await rig.store.get(session_id)
    assert row is not None
    return row


async def follow(rig: Rig, session_id: str = SESSION) -> None:
    """Open the conversation while nothing is new: followed, nothing read."""
    connection = rig.app(subscribed=[session_id])
    reply_frame = await rig.get(connection, session_id)
    assert reply_frame["result"]["status"]["state"] == "current"
    rig.service.on_app_detached(connection)


def behind(rig: Rig, count: int = 2, session_id: str = SESSION, start: int = 1) -> int:
    """The session ran ``count`` more rounds; returns its last seq."""
    events = rounds(count, start=start)
    rig.host.history.setdefault(session_id, []).extend(events)
    last = int(events[-1]["seq"])
    rig.sessions.put(session_id, last_seq=last)
    return last


def revision(number: int) -> Callable[[BoardRow], bool]:
    return lambda row: row.revision == number


# ---- triggers ----


async def test_a_followed_sessions_turn_end_is_read_twenty_seconds_later_unwatched(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    await follow(rig)
    rig.model.script = [ADD_FIX]
    last = behind(rig)
    rig.turn_ends(last_seq=last)
    await rig.advance(TURN_END_DEBOUNCE_SECONDS - 1)
    assert rig.host.history_reads() == [] and rig.model.calls == []
    await rig.advance(1)
    row = await stored(rig, revision(1))
    assert len(rig.host.history_reads()) == 1 and len(rig.model.calls) == 1
    assert [item.text for item in row.items] == ["Fix the login bug"]
    assert row.through_seq == last and row.checked_seq == last


async def test_a_turn_starting_again_inside_the_debounce_cancels_the_update(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    await follow(rig)
    rig.turn_ends(last_seq=behind(rig))
    await rig.advance(10)
    rig.turn_starts()
    await rig.advance(TURN_END_DEBOUNCE_SECONDS)
    assert rig.host.history_reads() == []


async def test_a_session_never_opened_is_never_read(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.turn_ends(last_seq=behind(rig))
    await rig.advance(TURN_END_DEBOUNCE_SECONDS + 1)
    assert rig.host.asks == []


async def test_a_session_not_opened_for_a_week_is_never_read(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await follow(rig)
    rig.clock.now += 7 * 24 * 3600 + 1
    rig.turn_ends(last_seq=behind(rig))
    await rig.advance(TURN_END_DEBOUNCE_SECONDS + 1)
    assert rig.host.asks == []


async def test_hundreds_of_idle_sessions_in_a_hello_cause_no_reads(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    for number in range(360):
        session_id = f"00000000-0000-4000-8000-{number:012d}"
        rig.host.own(session_id)
        rig.turn_ends(session_id, last_seq=behind(rig, 1, session_id))
    rig.service.on_device_online(DEVICE)
    await rig.advance(TURN_END_DEBOUNCE_SECONDS + MIN_INTERVAL_SECONDS)
    assert rig.host.asks == [] and rig.model.calls == []


async def test_a_hello_updates_followed_boards_behind_one_read_and_four_calls_at_a_time(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    sessions = [f"00000000-0000-4000-8000-{number:012d}" for number in range(6)]
    for session_id in sessions:
        rig.host.own(session_id)
        rig.sessions.put(session_id)
        await follow(rig, session_id)
        behind(rig, 1, session_id)
    rig.host.gate, rig.model.gate = asyncio.Event(), asyncio.Event()
    rig.service.on_device_online(DEVICE)
    await settle()
    assert rig.host.reading == 1
    rig.host.gate.set()
    await until(lambda: rig.model.active == CONCURRENT_CALLS)
    await settle()
    assert rig.model.active == CONCURRENT_CALLS
    rig.model.gate.set()
    for session_id in sessions:
        # One counter numbers every board of the store, so each has a revision of its own.
        await stored(rig, lambda row: row.revision > 0, session_id)
    assert rig.host.most_reading == 1 and rig.model.most_active == CONCURRENT_CALLS
    assert len(rig.host.history_reads()) == len(sessions)


async def test_opening_a_board_behind_reads_once_and_calls_once(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 3)
    rig.model.script = [ADD_FIX]
    connection = rig.app()
    first = await rig.get(connection)
    assert first["result"] == {"board": None, "status": {"state": "updating", "reason": None}}
    last = await told(rig, connection, "current")
    read = rig.host.history_reads()
    assert read == [
        {
            "type": "session.history",
            "session_id": SESSION,
            "limit": HISTORY_PAGE_EVENTS,
            "brief": True,
        }
    ]
    assert last["status"] == {"state": "current", "reason": None}
    assert last["board"]["revision"] == 1 and last["board"]["items"][0]["id"] == "t1"
    # Opened again with nothing new: the stored board, and nothing read or called.
    again = await rig.get(connection)
    assert again["result"]["status"]["state"] == "current"
    assert again["result"]["board"] == last["board"]
    await settle()
    assert len(rig.host.history_reads()) == 1 and len(rig.model.calls) == 1


async def test_nothing_new_in_what_was_read_calls_nothing_and_keeps_the_cursors(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    last = behind(rig, 2)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    # The session moved on by events history does not hold, a status or a meta.
    rig.sessions.put(last_seq=last + 3)
    await rig.advance(MIN_INTERVAL_SECONDS)
    sent = len(rig.host.updates(connection))
    reply_frame = await rig.refresh(connection)
    assert reply_frame["result"]["status"]["state"] == "updating"
    await told(rig, connection, "current", after=sent)
    assert len(rig.host.history_reads()) == 2 and len(rig.model.calls) == 1
    row = await row_of(rig)
    assert (row.revision, row.checked_seq, row.through_seq) == (1, last + 3, last)


async def test_the_pages_go_back_only_as_far_as_the_selection_needs(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 300)
    await rig.get(rig.app())
    await stored(rig, revision(1))
    pages = rig.host.history_reads()
    # Ten rounds of three events fit in the newest page.
    assert len(pages) == 1
    rig2 = await make_rig(tmp_path / "long")
    rig2.host.history[SESSION] = [person(1, "one long request")] + [
        reply(n, "still going") for n in range(2, 1000)
    ]
    rig2.sessions.put(last_seq=999)
    await rig2.get(rig2.app())
    row = await stored(rig2, revision(1))
    asked = rig2.host.history_reads()
    assert len(asked) == HISTORY_PAGES
    assert [ask.get("before_seq") for ask in asked] == [None, 800, 600]
    assert row.partial is True


# ---- pacing ----


async def test_one_update_at_a_time_and_a_trigger_meanwhile_runs_once_more(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    rig.model.gate = asyncio.Event()
    connection = rig.app()
    await rig.get(connection)
    await until(lambda: len(rig.model.calls) == 1)
    behind(rig, 1, start=10)
    for _ in range(3):
        assert (await rig.refresh(connection))["result"]["status"]["state"] == "updating"
    rig.model.gate.set()
    await stored(rig, revision(1))
    assert len(rig.model.calls) == 1
    await rig.advance(MIN_INTERVAL_SECONDS)
    await stored(rig, revision(2))
    await rig.advance(MIN_INTERVAL_SECONDS)
    assert len(rig.model.calls) == 2


async def test_two_calls_for_a_session_are_a_minute_apart(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    await follow(rig)
    rig.turn_ends(last_seq=behind(rig))
    await rig.advance(TURN_END_DEBOUNCE_SECONDS)
    await stored(rig, revision(1))
    rig.turn_ends(last_seq=behind(rig, start=20))
    await rig.advance(TURN_END_DEBOUNCE_SECONDS)
    assert len(rig.model.calls) == 1
    await rig.advance(MIN_INTERVAL_SECONDS - TURN_END_DEBOUNCE_SECONDS)
    await stored(rig, revision(2))
    assert len(rig.model.calls) == 2


async def test_past_the_hourly_cap_an_account_is_rate_limited(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.service._ctx.hourly.limit = 1
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    behind(rig, 1, start=10)
    await rig.advance(MIN_INTERVAL_SECONDS)
    status = (await rig.refresh(connection))["result"]["status"]
    assert status["state"] == "stale" and status["reason"] == "rate_limited"
    assert status["retry_at"] == int(rig.clock.now * 1000) - MIN_INTERVAL_SECONDS * 1000 + 3600_000
    assert len(rig.model.calls) == 1


async def test_failures_back_off_one_five_and_thirty_minutes(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.model.script = [ModelFailure("down")] * 4
    behind(rig, 1)
    connection = rig.app()
    waits: list[int] = []
    await rig.get(connection)
    for attempt in range(4):
        failures = attempt + 1
        row = await stored(rig, lambda row: row.failures == failures)  # noqa: B023
        waits.append((row.retry_at or 0) // 1000 - int(rig.clock.now))
        expected = {"state": "stale", "reason": "model_failed", "retry_at": row.retry_at}
        await until(lambda: rig.host.updates(connection)[-1]["status"] == expected)  # noqa: B023
        # A turn end inside the back-off is ignored; Refresh is not.
        rig.turn_ends(last_seq=row.checked_seq + 100)
        await rig.advance(TURN_END_DEBOUNCE_SECONDS)
        assert len(rig.model.calls) == attempt + 1
        await rig.advance(MIN_INTERVAL_SECONDS - TURN_END_DEBOUNCE_SECONDS)
        if attempt < 3:
            await rig.refresh(connection)
    assert waits == [*BACKOFF_SECONDS, BACKOFF_SECONDS[-1]]
    # A success clears the count.
    await rig.advance(BACKOFF_SECONDS[-1])
    await rig.refresh(connection)
    row = await stored(rig, revision(1))
    assert (row.failures, row.retry_at) == (0, None)


# ---- what can go wrong ----


async def test_an_offline_device_is_not_asked(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    rig.host.online.clear()
    status = (await rig.get(rig.app()))["result"]["status"]
    assert status == {"state": "stale", "reason": "device_offline", "retry_at": None}
    await settle()
    assert rig.host.asks == []


async def test_history_that_does_not_come_or_comes_too_large_is_unavailable(
    tmp_path: Path,
) -> None:
    for failing in ("silent", "error", "large"):
        rig = await make_rig(tmp_path / failing)
        behind(rig, 1)
        rig.host.failing = failing
        connection = rig.app()
        await rig.get(connection)
        await until(lambda: bool(rig.host.updates()))  # noqa: B023
        await settle()
        status = rig.host.updates(connection)[-1]["status"]
        assert (status["state"], status["reason"]) == ("stale", "history_unavailable"), failing
        assert rig.model.calls == []
        assert (await row_of(rig)).failures == 1


async def test_an_answer_that_is_not_a_board_changes_nothing(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.model.script = [ADD_FIX, "Sure! Here are the tasks: none."]
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    before = await row_of(rig)
    behind(rig, 1, start=10)
    await rig.advance(MIN_INTERVAL_SECONDS)
    await rig.refresh(connection)
    after = await stored(rig, lambda row: row.failures == 1)
    assert (after.items, after.through_seq, after.revision) == (
        before.items,
        before.through_seq,
        before.revision,
    )
    await until(lambda: rig.host.updates(connection)[-1]["status"]["reason"] == "invalid_output")
    # The same rounds are read again next time.
    rig.model.script = [ADD_FIX]
    await rig.advance(BACKOFF_SECONDS[0])
    await rig.refresh(connection)
    await stored(rig, revision(2))
    assert "request 1" in rig.model.conversation()


async def test_an_update_that_changes_no_item_is_still_a_new_revision(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.model.script = [ADD_FIX, answer(("t1", "todo", "Fix the login bug"))]
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    behind(rig, 1, start=10)
    await rig.advance(MIN_INTERVAL_SECONDS)
    sent = len(rig.host.updates(connection))
    await rig.refresh(connection)
    await told(rig, connection, "current", after=sent)
    row = await row_of(rig)
    assert row.revision == 2 and [item.id for item in row.items] == ["t1"]
    revisions = [frame["board"]["revision"] for frame in rig.host.updates(connection)]
    assert revisions == sorted(revisions) and revisions[-1] == 2


async def test_start_over_clears_the_board_keeps_its_ids_and_reads_again(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    rig.model.script = [ADD_FIX, answer((None, "doing", "Fix the login bug"))]
    behind(rig, 2)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    await rig.advance(MIN_INTERVAL_SECONDS)
    reply_frame = await rig.refresh(connection, rebuild=True)
    assert reply_frame["result"]["status"]["state"] == "updating"
    row = await stored(rig, revision(3))
    assert [(item.id, item.status) for item in row.items] == [("t2", "doing")]
    assert row.revision == 3 and row.next_item == 3
    # It read the newest rounds again, from the start.
    assert (
        "request 1" in rig.model.conversation()
        and "<board>\n[]\n</board>" in rig.model.conversation()
    )


# ---- the permission ----


async def test_with_the_board_off_an_open_is_off_and_reads_nothing(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, board_on=False)
    behind(rig, 2)
    connection = rig.app()
    assert (await rig.get(connection))["result"]["status"] == {"state": "off", "reason": None}
    assert (await rig.refresh(connection))["result"]["status"] == {"state": "off", "reason": None}
    await settle()
    assert rig.host.asks == [] and await rig.store.get(SESSION) is None


async def test_turning_the_board_off_deletes_every_board_and_tells_the_watchers(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    await rig.service.on_preferences(OWNER, rig.preferences.turn(False))
    assert await rig.store.get(SESSION) is None
    assert rig.host.updates(connection)[-1] == {
        "type": "board.updated",
        "session_id": SESSION,
        "device_id": DEVICE,
        "board": None,
        "status": {"state": "off", "reason": None},
    }
    rig.turn_ends(last_seq=behind(rig, 1, start=10))
    await rig.advance(TURN_END_DEBOUNCE_SECONDS + MIN_INTERVAL_SECONDS)
    assert len(rig.host.history_reads()) == 1


async def test_turning_the_board_on_updates_what_the_apps_have_open(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, board_on=False)
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    await rig.service.on_preferences(OWNER, rig.preferences.turn(True))
    await told(rig, connection, "current")
    assert (await row_of(rig)).revision == 1


# ---- revisions, and what a deletion stops ----


async def test_revisions_only_grow_across_turning_the_board_off_and_on(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    first = await told(rig, connection, "current")
    await rig.service.on_preferences(OWNER, rig.preferences.turn(False))
    off = len(rig.host.updates(connection))
    await rig.service.on_preferences(OWNER, rig.preferences.turn(True))
    again = await told(rig, connection, "current", after=off)
    assert again["board"]["revision"] > first["board"]["revision"]


async def test_revisions_only_grow_across_start_over(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    connection = rig.app()
    await rig.get(connection)
    await told(rig, connection, "current")
    await rig.advance(MIN_INTERVAL_SECONDS)
    sent = len(rig.host.updates(connection))
    await rig.refresh(connection, rebuild=True)
    await told(rig, connection, "current", after=sent)
    revisions = [frame["board"]["revision"] for frame in rig.host.updates(connection)]
    assert revisions == sorted(set(revisions)) and len(revisions) == 3


async def test_an_update_that_ends_after_the_board_was_turned_off_writes_and_sends_nothing(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    rig.model.gate = asyncio.Event()
    connection = rig.app()
    await rig.get(connection)
    await until(lambda: len(rig.model.calls) == 1)
    await rig.service.on_preferences(OWNER, rig.preferences.turn(False))
    told_off = rig.host.updates(connection)
    rig.model.gate.set()
    await settle()
    assert await rig.store.get(SESSION) is None
    assert rig.host.updates(connection) == told_off
    assert told_off[-1]["status"]["state"] == "off"


async def test_an_update_that_ends_after_its_session_left_writes_and_sends_nothing(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    rig.model.gate = asyncio.Event()
    connection = rig.app()
    await rig.get(connection)
    await until(lambda: len(rig.model.calls) == 1)
    sent = rig.host.updates(connection)
    rig.service.on_session_dropped(SESSION)
    rig.model.gate.set()
    await settle()
    assert await rig.store.get(SESSION) is None
    assert rig.host.updates(connection) == sent


async def test_the_sweep_keeps_a_board_a_socket_watches(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    connection = rig.app()
    await rig.get(connection)
    rig.clock.now += 31 * 24 * 3600
    await rig.service.sweep()
    assert await rig.store.get(SESSION) is not None
    rig.service.on_app_detached(connection)
    await rig.service.sweep()
    assert await rig.store.get(SESSION) is None


# ---- who is told ----


async def test_board_updated_reaches_only_the_owners_sockets_still_subscribed(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    behind(rig, 1)
    rig.model.gate = asyncio.Event()
    watching, left, never = rig.app(), rig.app(), rig.app(subscribed=[])
    stranger = rig.app("grace")
    for connection in (watching, left, never):
        await rig.get(connection)
    refused = await rig.get(stranger)
    assert refused["ok"] is False and refused["error"]["code"] == "not_found"
    left.unsubscribe(SESSION)
    rig.model.gate.set()
    await told(rig, watching, "current")
    assert rig.host.updates(left) == rig.host.updates(never) == rig.host.updates(stranger) == []


async def test_requests_are_checked_before_anything_else(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    connection = rig.app()
    for frame, code in (
        ({"type": "board.get", "id": "a"}, "bad_request"),
        (
            {"type": "board.refresh", "id": "b", "session_id": SESSION, "rebuild": "yes"},
            "bad_request",
        ),
        ({"type": "board.get", "id": "c", "session_id": "unknown"}, "not_found"),
    ):
        await rig.service.handle_app_frame(connection, frame)
        assert rig.host.replies(connection)[-1]["error"]["code"] == code
    await rig.service.handle_app_frame(connection, {"type": "board.get", "session_id": SESSION})
    assert len(rig.host.replies(connection)) == 3
    assert await rig.store.get(SESSION) is None
