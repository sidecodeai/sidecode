"""Amendment A56: how a catch-up ends when it did not finish.

A connection of a device that is replaced takes its fetches with it: what they held is dropped, not
published, and whoever was behind is marked. The new connection's `hello` fetches again from every
subscriber's cursor, because the device still has every event, so nothing is carried from one
connection to the next and the replay buffer is never given events that skip what nobody fetched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
    subscribe,
    until,
)
from .conftest import device_hello, fake_app, fake_device, hub_rig, session_summary


@pytest.mark.asyncio
async def test_a_replaced_connections_fetch_is_dropped_and_its_subscribers_are_marked(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    first = await reconnect(rig, last_seq=60)
    assert await history_request(first) is not None
    await rig.hub.handle_device_frame(first, live(61))

    # The link flaps, and the new connection never says hello.
    await rig.hub.attach_device(fake_device(rig.device_id))
    await asyncio.sleep(0.05)

    assert rig.hub._catchups.held(SESSION_ID) is None
    assert watching.events() == []
    assert SESSION_ID not in rig.hub._buffers
    assert rig.app.is_stale(SESSION_ID)
    await subscribe(rig, since=50, identifier="again")
    assert watching.reply("again")["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_the_replay_buffer_never_holds_a_hole_across_two_flaps(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=80)
    await subscribe(rig, since=80)
    first = await reconnect(rig, last_seq=81)
    assert await history_request(first) is not None
    await rig.hub.handle_device_frame(first, live(82))
    # The link flaps, and the device has gone on while it was down.
    second = fake_device(rig.device_id)
    await rig.hub.attach_device(second)
    await rig.hub.handle_device_hello(
        second,
        device_hello(sessions=[session_summary(SESSION_ID, rig.device_id, last_seq=112)]),
    )
    assert await history_request(second) is not None
    await rig.hub.handle_device_frame(second, live(113))
    # The link drops again before the device has answered.
    await rig.hub.detach_device(second)
    await until(lambda: rig.hub._catchups.held(SESSION_ID) is None)

    buffer = rig.hub._buffers.get(SESSION_ID)
    seqs = [] if buffer is None else [event["seq"] for event in buffer.tail(200)]
    if seqs:
        assert seqs == list(range(seqs[0], seqs[-1] + 1))
    # An app that holds 82 is not told that 113 is all that follows it.
    holder = fake_app()
    await rig.hub.attach_app(holder)
    await subscribe(rig, holder, since=82, identifier="holder")
    result = Watching(holder).reply("holder")["result"]
    assert result["resync"] is True or [e["seq"] for e in result["events"]] == list(range(83, 114))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_task_cancelled_before_its_first_step_leaves_no_hold_behind(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = fake_device(rig.device_id)
    await rig.hub.attach_device(revived)
    catchups = rig.hub._catchups
    summaries = [session_summary(SESSION_ID, rig.device_id, last_seq=60)]
    catchups.start(revived, summaries, {SESSION_ID: 50})
    assert catchups.held(SESSION_ID) is not None

    # The link drops before the task has taken its first step.
    catchups.cancel(revived)

    await until(lambda: catchups.held(SESSION_ID) is None)
    assert rig.app.is_stale(SESSION_ID)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_event_a_replaced_connection_still_delivers_is_not_published(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=80)
    await subscribe(rig, since=80)
    first = await reconnect(rig, last_seq=81)
    assert await history_request(first) is not None
    await rig.hub.handle_device_frame(first, live(82))
    second = fake_device(rig.device_id)
    await rig.hub.attach_device(second)

    # The old link had read this one before it was replaced.
    await rig.hub.handle_device_frame(first, live(83))

    assert watching.events() == []
    assert SESSION_ID not in rig.hub._buffers
    # The new connection's hello asks for all of it, from where the app is.
    summaries = [session_summary(SESSION_ID, rig.device_id, last_seq=83)]
    await rig.hub.handle_device_hello(second, device_hello(sessions=summaries))
    request = await history_request(second)
    assert request is not None and request["after_seq"] == 80
    await rig.hub.handle_device_frame(second, history_reply(request, blocks(81, 83)))
    await until(lambda: len(watching.events()) == 3)
    assert watching.events() == [81, 82, 83]
    await rig.hub.stop()
