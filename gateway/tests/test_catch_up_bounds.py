"""Amendment A56: what the catch-ups may send an app, and how long one may take.

An app's send queue holds a bounded number of frames, and a burst that overruns it costs the app
its socket. The catch-ups that send a socket events share what its queue can take, what the queue
already holds included, and a socket that would be sent more is marked and sent none of the fetch:
it pages history instead. The seconds a catch-up may take are counted from the moment it asks for
its turn.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from rc_gateway.connections import DeviceConnection

from .catch_up_support import (
    OTHER_ID,
    PATIENT,
    SESSION_ID,
    Watching,
    blocks,
    history_reply,
    history_request,
    live,
    put_session,
    reconnect,
    serve_history,
    slow_sends,
    subscribe,
    until,
)
from .conftest import HubRig, device_hello, fake_app, fake_device, hub_rig, session_summary

THIRD_ID = "9999aaaa-2222-4333-8444-555555555555"


@pytest.mark.asyncio
async def test_a_gap_longer_than_an_app_can_be_sent_at_once_is_left_to_resync(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=2550)
    for page in range(5):
        request = await history_request(revived)
        assert request is not None
        first = 51 + page * 500
        await rig.hub.handle_device_frame(
            revived, history_reply(request, blocks(first, first + 499), has_more=page < 4)
        )

    await until(lambda: SESSION_ID not in rig.hub._catchups._holds)

    assert rig.app.close_code is None
    assert rig.app.is_stale(SESSION_ID)
    assert watching.events() == []
    await rig.hub.stop()


@pytest.mark.parametrize(("cap", "delivered"), [(12, False), (13, True)])
@pytest.mark.asyncio
async def test_the_events_held_behind_a_fetch_count_towards_what_one_catch_up_may_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: int, delivered: bool
) -> None:
    monkeypatch.setattr("rc_gateway.catchup.CATCH_UP_EVENTS", cap)
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=63)
    request = await history_request(revived)
    assert request is not None
    for seq in (61, 62, 63):
        await rig.hub.handle_device_frame(revived, live(seq))
    # What the queue holds is counted against the socket, so it starts empty.
    watching.read()

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))

    await until(lambda: SESSION_ID not in rig.hub._catchups._holds)
    if delivered:
        await until(lambda: len(watching.events()) == 13)
        assert not rig.app.is_stale(SESSION_ID)
    else:
        # Nothing of the fetch goes out, and the live events go out as they would have.
        await until(lambda: watching.events() == [61, 62, 63])
        assert rig.app.is_stale(SESSION_ID)
    await rig.hub.stop()


async def _two_sessions_behind_one_socket(
    rig: HubRig, gap: int
) -> tuple[Watching, dict[str, int], DeviceConnection]:
    """One socket at 50 on two sessions of a device that comes back with ``gap`` more of each."""
    watching = Watching(rig.app)
    tips = {SESSION_ID: 50 + gap, OTHER_ID: 50 + gap}
    for session_id in tips:
        await put_session(rig, last_seq=50, session_id=session_id)
        await subscribe(rig, since=50, session_id=session_id, identifier=session_id)
    watching.read()
    revived = fake_device(rig.device_id)
    await rig.hub.attach_device(revived)
    summaries = [session_summary(item, rig.device_id, last_seq=tip) for item, tip in tips.items()]
    await rig.hub.handle_device_hello(revived, device_hello(sessions=summaries))
    return watching, tips, revived


@pytest.mark.asyncio
async def test_two_catch_ups_for_one_socket_share_what_its_queue_can_take(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching, tips, revived = await _two_sessions_behind_one_socket(rig, 1400)

    await serve_history(rig, revived, tips)
    await until(lambda: not rig.hub._catchups._holds)

    # One of them fits, and the other is left to resync: 2800 frames would not fit the queue.
    assert rig.app.close_code is None
    sizes = sorted(len(watching.events(item)) for item in tips)
    assert sizes == [0, 1400]
    assert sum(rig.app.is_stale(item) for item in tips) == 1
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_catch_ups_that_overlap_share_the_budget_of_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.catchup.CATCH_UP_EVENTS", 20)
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching, tips, revived = await _two_sessions_behind_one_socket(rig, 12)
    asked = [await history_request(revived), await history_request(revived)]
    assert None not in asked
    slow_sends(rig, monkeypatch)

    # Both replies are in before either catch-up has finished sending: 24 frames do not fit 20.
    for request in asked:
        assert request is not None
        reply = history_reply(request, blocks(51, tips[request["session_id"]]))
        await rig.hub.handle_device_frame(revived, reply)
    await until(lambda: not rig.hub._catchups._holds)

    assert sorted(len(watching.events(item)) for item in tips) == [0, 12]
    assert sum(rig.app.is_stale(item) for item in tips) == 1
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_socket_over_its_budget_does_not_keep_another_from_its_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.catchup.CATCH_UP_EVENTS", 15)
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    near = fake_app()
    await rig.hub.attach_app(near)
    far_watch, near_watch = Watching(rig.app), Watching(near)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    await subscribe(rig, near, since=60, identifier="near")
    far_watch.read()
    near_watch.read()
    revived = await reconnect(rig, last_seq=70)
    request = await history_request(revived)
    assert request is not None and request["after_seq"] == 50

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 70)))

    await until(lambda: len(near_watch.events()) == 10)
    assert near_watch.events() == list(range(61, 71))
    assert far_watch.events() == []
    assert rig.app.is_stale(SESSION_ID) and not near.is_stale(SESSION_ID)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_the_budget_of_a_catch_up_counts_its_wait_for_a_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=0.6, catch_up_budget=0.6)
    sessions = [SESSION_ID, OTHER_ID, THIRD_ID]
    for session_id in sessions:
        await put_session(rig, last_seq=50, session_id=session_id)
        await subscribe(rig, since=50, session_id=session_id, identifier=session_id)
    revived = fake_device(rig.device_id)
    await rig.hub.attach_device(revived)

    with caplog.at_level(logging.INFO, logger="rc_gateway.catchup"):
        summaries = [session_summary(item, rig.device_id, last_seq=60) for item in sessions]
        await rig.hub.handle_device_hello(revived, device_hello(sessions=summaries))
        # A device serves two fetches at a time, so the third session waits for one of them.
        asked = [await history_request(revived), await history_request(revived)]
        assert None not in asked
        await asyncio.sleep(0.3)
        for request in asked:
            assert request is not None
            await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
        # Nobody answers the third, which runs out of what was left of the budget.
        assert await history_request(revived) is not None

        def failed() -> list[logging.LogRecord]:
            return [
                item
                for item in caplog.records
                if item.getMessage() == "session caught up"
                and item.fields["outcome"] == "fetch_failed"  # type: ignore[attr-defined]
            ]

        await until(lambda: len(failed()) == 1)

    assert failed()[0].fields["ms"] < 750  # type: ignore[attr-defined]
    await rig.hub.stop()
