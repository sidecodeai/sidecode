"""Amendment A56: a socket that missed events the gateway could not fetch keeps its mark.

The mark tells the socket's next `session.subscribe` to answer `resync`. A catch-up clears it only
when it covered what was missing, and one that is cut short while it publishes leaves it, so that
no hole in a transcript goes unnoticed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rc_gateway.connections import AppConnection

from .catch_up_support import (
    PATIENT,
    SESSION_ID,
    Watching,
    blocks,
    history_reply,
    history_request,
    live,
    put_session,
    reconnect,
    slow_sends,
    subscribe,
    until,
)
from .conftest import HubRig, device_hello, fake_app, fake_device, hub_rig, session_summary


def test_a_mark_is_cleared_only_by_a_fetch_that_began_at_or_below_its_hole() -> None:
    app = fake_app()
    app.subscribe(SESSION_ID, 50)
    app.mark_stale(SESSION_ID)
    # The cursor moves on past the hole, and a second mark keeps the first hole.
    app.subscribe(SESSION_ID, 62)
    app.mark_stale(SESSION_ID)

    app.clear_stale(SESSION_ID, after=62)
    assert app.is_stale(SESSION_ID)
    app.clear_stale(SESSION_ID, after=50)
    assert not app.is_stale(SESSION_ID)


@pytest.mark.asyncio
async def test_a_later_catch_up_leaves_the_mark_of_a_gap_it_did_not_cover(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    first = await reconnect(rig, last_seq=60)
    assert await history_request(first) is not None
    await rig.hub.handle_device_frame(first, live(61))
    await rig.hub.handle_device_frame(first, live(62))
    # The link drops before the device answers: what was held goes out, past the hole at 51..60.
    await rig.hub.detach_device(first)
    await until(lambda: watching.events() == [61, 62] and rig.app.is_stale(SESSION_ID))

    # The device comes back with news, and that catch-up starts after where the app is now.
    second = await reconnect(rig, last_seq=64)
    request = await history_request(second)
    assert request is not None and request["after_seq"] == 62
    await rig.hub.handle_device_frame(second, history_reply(request, blocks(63, 64)))
    await until(lambda: watching.events() == [61, 62, 63, 64])

    await subscribe(rig, since=64, identifier="again")
    assert watching.reply("again")["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hold_that_overflows_while_the_fetch_is_published_keeps_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.catchup.HOLD_EVENTS", 3)
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None
    slow_sends(rig, monkeypatch)
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await asyncio.sleep(0.025)
    for seq in range(61, 65):
        await rig.hub.handle_device_frame(revived, live(seq))

    await until(lambda: SESSION_ID not in rig.hub._catchups._holds)
    await asyncio.sleep(0.2)

    assert rig.app.is_stale(SESSION_ID)
    await subscribe(rig, since=64, identifier="again")
    assert (await watching.wait_reply("again"))["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_connection_replaced_while_it_publishes_leaves_the_subscriber_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    first = await reconnect(rig, last_seq=62)
    request = await history_request(first)
    assert request is not None
    queue = {"seq": 63, "ts": 63, "kind": "queue", "pending": []}
    delta = {"seq": 64, "ts": 64, "kind": "assistant_text", "block_id": "s", "text": "x"}
    for event in (queue, delta):
        frame = {"type": "session.event", "session_id": SESSION_ID, "event": event}
        await rig.hub.handle_device_frame(first, frame)
    slow_sends(rig, monkeypatch)
    await rig.hub.handle_device_frame(first, history_reply(request, blocks(51, 60)))
    # Two events are out and a third is on its way when the link flaps.
    await asyncio.sleep(0.025)
    second = fake_device(rig.device_id)
    await rig.hub.attach_device(second)

    await rig.hub.handle_device_hello(
        second, device_hello(sessions=[session_summary(SESSION_ID, rig.device_id, last_seq=64)])
    )
    request = await history_request(second)
    assert request is not None
    await rig.hub.handle_device_frame(
        second, history_reply(request, blocks(request["after_seq"] + 1, 60))
    )
    await until(lambda: 60 in watching.events() and SESSION_ID not in rig.hub._catchups._holds)

    await subscribe(rig, since=64, identifier="again")
    assert (await watching.wait_reply("again"))["result"]["resync"] is True
    await rig.hub.stop()


async def _level_and_behind_sockets(rig: HubRig) -> tuple[Watching, Watching, AppConnection]:
    """One socket level with the device's hello and one behind it, on the same session."""
    behind = fake_app()
    await rig.hub.attach_app(behind)
    await put_session(rig, last_seq=60)
    rig.app.subscribe(SESSION_ID, 60)
    behind.subscribe(SESSION_ID, 50)
    return Watching(rig.app), Watching(behind), behind


@pytest.mark.asyncio
async def test_a_pass_cut_short_by_a_link_drop_marks_the_socket_level_with_the_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    level, _, behind = await _level_and_behind_sockets(rig)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None and request["after_seq"] == 50
    # 61 is held and is also in the reply, where the fetched copy takes its place.
    await rig.hub.handle_device_frame(revived, live(61))
    slow_sends(rig, monkeypatch)
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 61)))
    await asyncio.sleep(0.025)
    await rig.hub.handle_device_frame(revived, live(62))

    # The link drops while the pass is publishing.
    await rig.hub.detach_device(revived)
    await until(lambda: rig.hub._catchups.held(SESSION_ID) is None)
    await asyncio.sleep(0.1)

    # The held copy of 61 was taken in by the fetched one, which the pass never reached.
    assert level.events() == [61, 62]
    assert rig.app.is_stale(SESSION_ID)
    assert behind.is_stale(SESSION_ID)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_pass_cut_short_by_an_overflow_marks_the_socket_level_with_the_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.catchup.HOLD_EVENTS", 3)
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    level, _, behind = await _level_and_behind_sockets(rig)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None
    for seq in (61, 62):
        await rig.hub.handle_device_frame(revived, live(seq))
    slow_sends(rig, monkeypatch)
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await asyncio.sleep(0.025)

    # Three more fill the hold and the fourth lets go of it, while the pass is publishing.
    for seq in range(63, 67):
        await rig.hub.handle_device_frame(revived, live(seq))
    await until(lambda: rig.hub._catchups.held(SESSION_ID) is None)
    await asyncio.sleep(0.2)

    assert level.events() == [63, 64, 65, 66]
    assert rig.app.is_stale(SESSION_ID)
    assert behind.is_stale(SESSION_ID)
    await rig.hub.stop()
