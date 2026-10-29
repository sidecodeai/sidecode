"""Amendment A56: the gateway fetches from a device what an app is missing.

A restart, an evicted replay buffer or a dropped link leaves the gateway without events an app is
waiting for, and the device holds every one of them. These tests drive the hub directly, with fake
connections whose queues are read by hand, so the order of every frame is the order the hub wrote.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.catchup import fetch_missing
from rc_gateway.connections import AppConnection, DeviceConnection
from rc_gateway.frames import REQUEST_TIMEOUT_SECONDS
from rc_gateway.hub import SessionResumeNotice

from .catch_up_support import (
    IMPATIENT,
    PATIENT,
    SESSION_ID,
    Watching,
    block,
    blocks,
    history_reply,
    history_request,
    live,
    next_request,
    put_session,
    reconnect,
    refusal,
    subscribe,
    until,
)
from .conftest import (
    another_device,
    device_hello,
    fake_app,
    fake_device,
    frames_of,
    hub_rig,
    session_summary,
)

# ---- a subscribe the buffer cannot serve is answered from the device (A56 2.3a) ----


@pytest.mark.asyncio
async def test_a_subscribe_the_buffer_cannot_serve_is_answered_from_the_device(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await subscribe(rig, since=50)

    request = await history_request(rig.device)
    assert request is not None
    assert (request["after_seq"], request["limit"], request["from"]) == (50, 500, "gateway")
    assert request["session_id"] == SESSION_ID
    assert request["device_id"] == rig.device_id
    await rig.hub.handle_device_frame(rig.device, history_reply(request, blocks(51, 60)))

    result = (await watching.wait_reply("s1"))["result"]
    assert [event["seq"] for event in result["events"]] == list(range(51, 61))
    assert result["resync"] is False
    assert result["session"]["last_seq"] == 60
    # The subscriber is registered where the reply left it, so the next live event is its only one.
    await rig.hub.handle_device_frame(rig.device, live(61))
    assert watching.events() == [61]
    await rig.hub.stop()


@pytest.mark.parametrize("reply_to", [(51, 60), (51, 61)])
@pytest.mark.asyncio
async def test_a_live_event_during_the_fetch_reaches_the_new_subscriber_once(
    tmp_path: Path, reply_to: tuple[int, int]
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await subscribe(rig, since=50)
    request = await history_request(rig.device)
    assert request is not None

    # The event lands before the device's answer, which may or may not already include it.
    await rig.hub.handle_device_frame(rig.device, live(61))
    await rig.hub.handle_device_frame(rig.device, history_reply(request, blocks(*reply_to)))

    delivered = [event["seq"] for event in (await watching.wait_reply("s1"))["result"]["events"]]
    assert delivered == list(range(51, 62))
    await rig.hub.handle_device_frame(rig.device, live(62))
    assert watching.events() == [62]
    await rig.hub.stop()


@pytest.mark.parametrize(
    "failure", ["refused", "has_more", "too_many_events", "too_many_bytes", "silence"]
)
@pytest.mark.asyncio
async def test_a_fetch_that_does_not_fit_leaves_the_app_to_resync(
    tmp_path: Path, failure: str
) -> None:
    # Only the device that never answers needs the short wait.
    rig = await hub_rig(tmp_path, fetch_timeout=IMPATIENT if failure == "silence" else PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=700)
    await subscribe(rig, since=50)
    request = await history_request(rig.device)
    assert request is not None

    replies = {
        "refused": refusal(request),
        "has_more": history_reply(request, blocks(51, 150), has_more=True),
        "too_many_events": history_reply(request, blocks(51, 551)),
        "too_many_bytes": history_reply(
            request, [block(seq, "x" * 60_000) for seq in range(51, 71)]
        ),
    }
    if failure in replies:
        await rig.hub.handle_device_frame(rig.device, replies[failure])

    result = (await watching.wait_reply("s1"))["result"]
    assert result["resync"] is True
    assert result["events"] == []
    assert result["session"]["last_seq"] == 700
    assert rig.app.cursors[SESSION_ID] == 700
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_nothing_is_asked_when_the_gateway_already_has_what_the_app_needs(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)

    await subscribe(rig, since=60, identifier="current")
    await subscribe(rig, identifier="cold")

    assert await history_request(rig.device, timeout=0.1) is None
    assert watching.reply("current")["result"]["resync"] is False
    assert watching.reply("cold")["result"]["events"] == []
    # A subscribe with no cursor pages history itself, and is owed what follows what the gateway
    # holds, as any other socket is.
    assert rig.app.cursors[SESSION_ID] == 60
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_nothing_is_asked_of_a_device_that_is_not_connected(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await rig.hub.detach_device(rig.device)

    await subscribe(rig, since=50)

    assert await history_request(rig.device, timeout=0.1) is None
    assert watching.reply("s1")["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_session_of_another_account_asks_its_device_nothing(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    stranger = fake_app("mallory")
    await rig.hub.attach_app(stranger)
    await put_session(rig, last_seq=60)

    await subscribe(rig, stranger, since=50)

    assert await history_request(rig.device, timeout=0.1) is None
    (reply,) = frames_of(stranger)
    assert reply["error"]["code"] == "not_found"
    await rig.hub.stop()


# ---- a subscribe that waits for the device does not hold up its socket (A56) ----


@pytest.mark.asyncio
async def test_a_subscribe_that_waits_for_the_device_does_not_hold_up_the_socket(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)

    started = time.monotonic()
    await subscribe(rig, since=50)
    # The next frame on the socket is a Stop, a safety control: it is not read after the fetch.
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.stop", "id": "stop-1", "session_id": SESSION_ID}
    )

    assert time.monotonic() - started < 1.0
    stop = await next_request(rig.device, "session.stop", timeout=0.5)
    request = await history_request(rig.device)
    assert stop is not None and stop["id"] == "stop-1"
    assert request is not None
    assert not [item for item in watching.read() if item.get("id") == "s1"]
    await rig.hub.handle_device_frame(rig.device, history_reply(request, blocks(51, 60)))
    reply = await watching.wait_reply("s1")
    assert [event["seq"] for event in reply["result"]["events"]] == list(range(51, 61))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_appears_after_a_subscribe_looked_is_not_waited_for_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hello can land between a subscribe looking at its device and its acting on what it saw."""
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=60)
    connected = rig.hub._connected_device
    looks = 0

    def appears_late(device_id: str) -> DeviceConnection | None:
        nonlocal looks
        looks += 1
        return None if looks == 1 else connected(device_id)

    monkeypatch.setattr(rig.hub, "_connected_device", appears_late)

    # The socket's read loop is not held for a device that has not answered.
    await asyncio.wait_for(subscribe(rig, since=50), timeout=1.0)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_subscribe_whose_device_connects_while_it_reads_is_answered_off_the_read_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await rig.hub.detach_device(rig.device)
    revived = fake_device(rig.device_id)
    read = rig.index.get
    connected = False

    async def read_then_connect(session_id: str) -> Any:
        nonlocal connected
        indexed = await read(session_id)
        if not connected:
            connected = True
            await rig.hub.attach_device(revived)
        return indexed

    monkeypatch.setattr(rig.index, "get", read_then_connect)

    await asyncio.wait_for(subscribe(rig, since=50), timeout=1.0)

    request = await history_request(revived)
    assert request is not None
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    reply = await watching.wait_reply("s1")
    assert [event["seq"] for event in reply["result"]["events"]] == list(range(51, 61))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_unsubscribe_ends_a_subscribe_that_is_waiting_for_the_device(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await subscribe(rig, since=50)
    request = await history_request(rig.device)
    assert request is not None

    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.unsubscribe", "session_id": SESSION_ID}
    )
    await rig.hub.handle_device_frame(rig.device, history_reply(request, blocks(51, 60)))
    await asyncio.sleep(0.1)

    assert [item for item in watching.read() if item["type"] in ("reply", "session.event")] == []
    assert SESSION_ID not in rig.app.cursors
    await rig.hub.handle_device_frame(rig.device, live(61))
    assert watching.events() == []
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_subscribes_to_one_session_are_answered_in_the_order_they_came(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await subscribe(rig, since=50, identifier="first")
    await subscribe(rig, since=50, identifier="second")

    for _ in range(2):
        request = await history_request(rig.device)
        assert request is not None
        await rig.hub.handle_device_frame(rig.device, history_reply(request, blocks(51, 60)))
    await watching.wait_reply("second")

    assert [item["id"] for item in watching.read() if item.get("type") == "reply"] == [
        "first",
        "second",
    ]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_closing_socket_gives_up_its_subscribes(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=60)
    await subscribe(rig, since=50)
    assert rig.app.subscribing[SESSION_ID]

    await rig.hub.detach_app(rig.app)

    assert rig.app.subscribing == {}
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_turn_that_does_not_come_in_time_gives_up_and_leaves_nothing_behind() -> None:
    from rc_gateway.catchup import FetchGate

    gate = FetchGate(per_device=1)
    async with gate.slot("device-1", "session-1"):
        with pytest.raises(TimeoutError):
            async with gate.slot("device-1", "session-2", within=0.05):
                raise AssertionError("the turn should not have come")
    assert gate._sessions == {} and gate._devices == {}


# ---- one fetch per session, two per device (A56 2.3d) ----


@pytest.mark.asyncio
async def test_one_fetch_per_session_is_in_flight_at_a_time(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    second = fake_app()
    await rig.hub.attach_app(second)
    await put_session(rig, last_seq=60)

    await subscribe(rig, since=50)
    await subscribe(rig, second, since=50, identifier="s2")
    first_request = await history_request(rig.device)
    assert first_request is not None
    assert await history_request(rig.device, timeout=0.1) is None

    await rig.hub.handle_device_frame(rig.device, history_reply(first_request, blocks(51, 60)))
    second_request = await history_request(rig.device)
    assert second_request is not None
    await rig.hub.handle_device_frame(rig.device, history_reply(second_request, blocks(51, 60)))

    for app, identifier in ((rig.app, "s1"), (second, "s2")):
        reply = await Watching(app).wait_reply(identifier)
        assert [event["seq"] for event in reply["result"]["events"]] == list(range(51, 61))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_serves_two_fetches_at_a_time(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    sessions = [f"{index:08x}-2222-4333-8444-555555555555" for index in range(3)]
    apps = [fake_app() for _ in sessions]
    for app, session_id in zip(apps, sessions, strict=True):
        await rig.hub.attach_app(app)
        await put_session(rig, last_seq=60, session_id=session_id)
    answers = [
        asyncio.ensure_future(subscribe(rig, app, since=50, session_id=session_id))
        for app, session_id in zip(apps, sessions, strict=True)
    ]

    first = await history_request(rig.device)
    second = await history_request(rig.device)
    assert first is not None and second is not None
    assert await history_request(rig.device, timeout=0.1) is None

    await rig.hub.handle_device_frame(rig.device, history_reply(first, blocks(51, 60)))
    third = await history_request(rig.device)
    assert third is not None
    await rig.hub.handle_device_frame(rig.device, history_reply(second, blocks(51, 60)))
    await rig.hub.handle_device_frame(rig.device, history_reply(third, blocks(51, 60)))
    await asyncio.gather(*answers)
    assert {request["session_id"] for request in (first, second, third)} == set(sessions)
    await rig.hub.stop()


# ---- a device that comes back brings its subscribers up to date (A56 2.3b) ----


@pytest.mark.asyncio
async def test_a_hello_fills_the_gap_an_app_subscribed_before_it_cannot_see(
    tmp_path: Path,
) -> None:
    """The hole after a restart: the app subscribed first, and the device came back with more."""
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    assert watching.reply("s1")["result"]["resync"] is False
    await rig.hub.detach_device(rig.device)

    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)

    assert request is not None
    assert (request["after_seq"], request["session_id"]) == (50, SESSION_ID)
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await until(lambda: len(watching.events()) == 10)
    assert watching.events() == list(range(51, 61))
    # What was fetched is buffered, so the next app to ask is served without a second round trip.
    late = fake_app()
    await rig.hub.attach_app(late)
    await subscribe(rig, late, since=50, identifier="late")
    (reply,) = [item for item in frames_of(late) if item.get("id") == "late"]
    assert [event["seq"] for event in reply["result"]["events"]] == list(range(51, 61))
    assert await history_request(revived, timeout=0.1) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_each_subscriber_is_given_what_follows_its_own_cursor(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    ahead = fake_app()
    await rig.hub.attach_app(ahead)
    first, second = Watching(rig.app), Watching(ahead)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    # An app that holds more than the gateway's index does, as one does after a crash that lost
    # the index's unwritten tail.
    await subscribe(rig, ahead, since=55, identifier="ahead")

    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None and request["after_seq"] == 50
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))

    await until(lambda: len(first.events()) == 10 and len(second.events()) == 5)
    assert first.events() == list(range(51, 61))
    assert second.events() == list(range(56, 61))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hello_asks_nothing_for_a_subscriber_that_is_not_behind(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=60)
    await subscribe(rig, since=60)
    cold = fake_app()
    await rig.hub.attach_app(cold)
    await subscribe(rig, cold, identifier="cold")

    revived = await reconnect(rig, last_seq=60)

    assert await history_request(revived, timeout=0.1) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hello_asks_nothing_for_a_session_nobody_watches(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=50)

    revived = await reconnect(rig, last_seq=900)

    assert await history_request(revived, timeout=0.1) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_live_event_waits_behind_the_events_being_fetched(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    watching.read()
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None

    await rig.hub.handle_device_frame(revived, live(61))
    await rig.hub.handle_device_frame(revived, live(62))
    assert watching.events() == []

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await until(lambda: len(watching.events()) == 12)
    assert watching.events() == list(range(51, 63))

    # The held events went through the buffer, so a late copy of one is turned away, and a
    # session that is no longer held passes its events straight through.
    await rig.hub.handle_device_frame(revived, live(61))
    await rig.hub.handle_device_frame(revived, live(63))
    assert watching.events() == list(range(51, 64))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_live_event_the_fetch_already_brought_is_not_delivered_twice(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None

    await rig.hub.handle_device_frame(revived, live(60))
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await until(lambda: len(watching.events()) >= 10)
    await asyncio.sleep(0.05)
    assert watching.events() == list(range(51, 61))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_subscriber_the_fetch_could_not_serve_is_resynced_when_it_asks_again(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=900)
    request = await history_request(revived)
    assert request is not None

    await rig.hub.handle_device_frame(revived, refusal(request))
    await rig.hub.handle_device_frame(revived, live(901))
    await until(lambda: watching.events() == [901])

    # What an app that noticed the hole does: subscribe again from where its timeline now ends.
    await subscribe(rig, since=901, identifier="repair")
    repaired = watching.reply("repair")["result"]
    assert repaired["resync"] is True and repaired["events"] == []

    # The mark is spent by that answer, and another connection never had one.
    await subscribe(rig, since=901, identifier="again")
    assert watching.reply("again")["result"]["resync"] is False
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_buffer_the_fetch_could_not_extend_is_dropped(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=2)
    for seq in (1, 2):
        await rig.hub.handle_device_frame(rig.device, live(seq))
    assert SESSION_ID in rig.hub._buffers

    revived = await reconnect(rig, last_seq=900)
    request = await history_request(revived)
    assert request is not None and request["after_seq"] == 2
    await rig.hub.handle_device_frame(revived, refusal(request))

    await until(lambda: SESSION_ID not in rig.hub._buffers)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_cannot_have_events_published_into_another_devices_session(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=2)
    await subscribe(rig, since=2)
    for seq in (1, 2):
        await rig.hub.handle_device_frame(rig.device, live(seq))
    other = await another_device(rig)

    await rig.hub.handle_device_hello(
        other, device_hello(sessions=[session_summary(SESSION_ID, other.device_id, last_seq=99)])
    )

    assert await history_request(other, timeout=0.1) is None
    assert watching.events() == [1, 2]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hold_that_fills_up_is_released_and_its_subscriber_resyncs(
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

    for seq in (61, 62, 63):
        await rig.hub.handle_device_frame(revived, live(seq))
    assert watching.events() == []
    await rig.hub.handle_device_frame(revived, live(64))
    # The device is not answering fast enough, so the session goes live rather than hold more.
    assert watching.events() == [61, 62, 63, 64]
    assert SESSION_ID not in rig.hub._catchups._holds

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await asyncio.sleep(0.05)
    assert watching.events() == [61, 62, 63, 64]
    await subscribe(rig, since=64, identifier="repair")
    assert watching.reply("repair")["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_link_that_drops_during_the_fetch_releases_the_session(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    assert await history_request(revived) is not None
    await rig.hub.handle_device_frame(revived, live(61))

    await rig.hub.detach_device(revived)

    await until(lambda: SESSION_ID not in rig.hub._catchups._holds)
    assert watching.events() == [61]
    await subscribe(rig, since=61, identifier="repair")
    assert watching.reply("repair")["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_only_an_event_the_gateway_had_not_heard_of_cues_a_push(tmp_path: Path) -> None:
    announced: list[str] = []

    async def on_resume(notice: SessionResumeNotice) -> None:
        announced.append(notice.status)

    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT, on_session_resume=on_resume)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    # An app that is further back than the gateway itself was.
    rig.app.subscribe(SESSION_ID, 45)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None and request["after_seq"] == 45

    old = {"seq": 48, "ts": 48, "kind": "resume", "status": "scheduled", "at": 1}
    new = {"seq": 55, "ts": 55, "kind": "resume", "status": "resumed", "at": 2}
    await rig.hub.handle_device_frame(revived, history_reply(request, [old, new]))

    await until(lambda: len(watching.events()) >= 2)
    await rig.hub.stop()
    assert announced == ["resumed"]


# ---- what a hold may not lose, and who is told when something is ----


def _event_frame(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": "session.event", "session_id": SESSION_ID, "event": event}


@pytest.mark.asyncio
async def test_a_live_event_that_history_does_not_carry_keeps_its_place_between_the_fetched_ones(
    tmp_path: Path,
) -> None:
    """Up next is republished right after a hello, inside the window of a fetch."""
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=62)
    request = await history_request(revived)
    assert request is not None
    queue = {
        "seq": 63,
        "ts": 63,
        "kind": "queue",
        "pending": [{"id": "q1", "text": "then this", "ts": 63}],
    }
    delta = {
        "seq": 64,
        "ts": 64,
        "kind": "assistant_text",
        "block_id": "streaming",
        "text": "a chunk",
        "done": False,
    }
    call = {"seq": 65, "ts": 65, "kind": "tool_call", "block_id": "call", "tool_kind": "shell"}
    for event in (queue, delta, call):
        await rig.hub.handle_device_frame(revived, _event_frame(event))
    assert watching.events() == []

    await rig.hub.handle_device_frame(revived, history_reply(request, [*blocks(51, 60), call]))

    await until(lambda: len(watching.events()) == 13)
    assert watching.events() == [*range(51, 61), 63, 64, 65]
    assert rig.hub._queues[SESSION_ID]["pending"] == queue["pending"]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_socket_that_subscribes_during_the_fetch_is_resynced_when_the_fetch_fails(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    late = fake_app()
    await rig.hub.attach_app(late)
    early_watch, late_watch = Watching(rig.app), Watching(late)
    await put_session(rig, last_seq=50)
    for seq in range(41, 51):
        await rig.hub.handle_device_frame(rig.device, live(seq))
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None

    # The restart brings the apps back together, so one arrives while the device is being asked.
    await subscribe(rig, late, since=50, identifier="late")
    assert late_watch.reply("late")["result"]["resync"] is False
    await rig.hub.handle_device_frame(revived, refusal(request))
    await rig.hub.handle_device_frame(revived, live(61))
    await until(lambda: early_watch.events() == [61] and late_watch.events() == [61])

    for app, watching in ((rig.app, early_watch), (late, late_watch)):
        await subscribe(rig, app, since=61, identifier="repair")
        assert watching.reply("repair")["result"]["resync"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_second_hello_fetches_what_the_connection_it_replaced_was_asked_for(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    first = await reconnect(rig, last_seq=60)
    assert await history_request(first) is not None
    await rig.hub.handle_device_frame(first, live(61))

    # The link flaps and the first connection's handler has not unwound yet. Nothing the first
    # one held is carried over: the device has every event, and this hello asks for them again.
    second = await reconnect(rig, last_seq=61)
    request = await history_request(second)

    assert request is not None and request["after_seq"] == 50
    await rig.hub.handle_device_frame(second, history_reply(request, blocks(51, 61)))
    await until(lambda: len(watching.events()) == 11)
    assert watching.events() == list(range(51, 62))
    assert not rig.app.is_stale(SESSION_ID)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_socket_that_pages_history_itself_is_sent_what_a_device_outage_missed(
    tmp_path: Path,
) -> None:
    """The app subscribed with no cursor while the device was up, then the device's link flapped."""
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=60)
    await subscribe(rig, identifier="cold")
    watching.read()

    revived = await reconnect(rig, last_seq=70)
    request = await history_request(revived)

    assert request is not None and request["after_seq"] == 60
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(61, 70)))
    await rig.hub.handle_device_frame(revived, live(71))
    await until(lambda: len(watching.events()) == 11)
    assert watching.events() == list(range(61, 72))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_socket_that_pages_history_itself_is_sent_what_follows_the_gateways_last_seq(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    cold = fake_app()
    await rig.hub.attach_app(cold)
    watching, cold_watch = Watching(rig.app), Watching(cold)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    await subscribe(rig, cold, identifier="cold")
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))

    await until(lambda: len(watching.events()) == 10 and len(cold_watch.events()) == 10)
    assert cold_watch.events() == list(range(51, 61))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_error_while_publishing_marks_every_subscriber_instead_of_dropping_events(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    holder = fake_app()
    await rig.hub.attach_app(holder)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    # An app that already holds everything the device has, and so is behind nothing.
    await subscribe(rig, holder, since=60, identifier="holder")
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None
    host = rig.hub._catchups._host
    publish = host.publish

    async def failing(
        device: DeviceConnection,
        session_id: str,
        event: dict[str, Any],
        frame: dict[str, Any],
        *,
        known: int | None = None,
        skip: frozenset[AppConnection] = frozenset(),
    ) -> bool:
        if event.get("seq") == 55:
            raise RuntimeError("the store went away")
        return await publish(device, session_id, event, frame, known=known, skip=skip)

    rig.hub._catchups._host = dataclasses.replace(host, publish=failing)
    await rig.hub.handle_device_frame(revived, live(61))
    await rig.hub.handle_device_frame(revived, live(62))

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))

    await until(lambda: rig.app.is_stale(SESSION_ID))
    assert holder.is_stale(SESSION_ID)
    assert SESSION_ID not in rig.hub._catchups._holds
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hold_that_overflows_while_the_fetch_is_applied_marks_the_subscriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.catchup.HOLD_EVENTS", 3)
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None
    host = rig.hub._catchups._host
    publish = host.publish

    async def slow(
        device: DeviceConnection,
        session_id: str,
        event: dict[str, Any],
        frame: dict[str, Any],
        *,
        known: int | None = None,
        skip: frozenset[AppConnection] = frozenset(),
    ) -> bool:
        await asyncio.sleep(0.01)
        return await publish(device, session_id, event, frame, known=known, skip=skip)

    rig.hub._catchups._host = dataclasses.replace(host, publish=slow)
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await asyncio.sleep(0.025)
    for seq in range(61, 65):
        await rig.hub.handle_device_frame(revived, live(seq))

    await until(lambda: SESSION_ID not in rig.hub._catchups._holds)
    await asyncio.sleep(0.15)
    assert rig.app.is_stale(SESSION_ID)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_later_catch_up_that_succeeds_clears_the_mark_a_failed_one_left(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    first = await reconnect(rig, last_seq=60)
    request = await history_request(first)
    assert request is not None
    await rig.hub.handle_device_frame(first, refusal(request))
    await until(lambda: rig.app.is_stale(SESSION_ID))

    second = await reconnect(rig, last_seq=60)
    request = await history_request(second)
    assert request is not None
    await rig.hub.handle_device_frame(second, history_reply(request, blocks(51, 60)))
    await until(lambda: len(watching.events()) == 10)

    await subscribe(rig, since=60, identifier="again")
    assert watching.reply("again")["result"]["resync"] is False
    await rig.hub.stop()


# ---- a gap of several pages ----


@pytest.mark.asyncio
async def test_a_gap_of_several_pages_is_delivered_only_when_the_last_page_is_in(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=1200)

    pages = [(51, 550, True, 50), (551, 1050, True, 550), (1051, 1200, False, 1050)]
    for first, last, more, after in pages:
        request = await history_request(revived)
        assert request is not None
        assert (request["after_seq"], request["limit"]) == (after, 500)
        assert watching.events() == []
        await rig.hub.handle_device_frame(
            revived, history_reply(request, blocks(first, last), has_more=more)
        )

    await until(lambda: len(watching.events()) == 1150)
    assert watching.events() == list(range(51, 1201))
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_gap_of_more_than_five_pages_is_left_to_resync(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=9000)

    for page in range(5):
        request = await history_request(revived)
        assert request is not None
        first = 51 + page * 500
        await rig.hub.handle_device_frame(
            revived, history_reply(request, blocks(first, first + 499), has_more=True)
        )

    await until(lambda: rig.app.is_stale(SESSION_ID))
    assert await history_request(revived, timeout=0.1) is None
    assert watching.events() == []
    await rig.hub.stop()


def test_a_page_with_more_to_come_but_nothing_in_it_is_no_progress() -> None:
    asked: list[float] = []

    async def ask(device: DeviceConnection, request: dict[str, Any], timeout: float) -> Any:
        asked.append(timeout)
        return {"type": "reply", "ok": True, "result": {"events": [], "has_more": True}}

    fetched = asyncio.run(fetch_missing(ask, fake_device("d"), SESSION_ID, 5, 1.0, pages=5))

    assert fetched is None and len(asked) == 1


def test_the_pages_of_one_fetch_share_a_budget() -> None:
    asked: list[float] = []

    async def ask(device: DeviceConnection, request: dict[str, Any], timeout: float) -> Any:
        asked.append(timeout)
        await asyncio.sleep(0.3)
        first = request["after_seq"] + 1
        return history_reply(
            {"id": "x", "from": "gateway"}, blocks(first, first + 1), has_more=True
        )

    fetched = asyncio.run(
        fetch_missing(ask, fake_device("d"), SESSION_ID, 0, 0.4, pages=5, budget=0.5)
    )

    assert fetched is None
    assert asked[0] == pytest.approx(0.4, abs=0.02)
    assert len(asked) == 2 and asked[1] < 0.25


@pytest.mark.parametrize("fault", ["too_many_events", "too_many_bytes"])
def test_a_page_over_its_bounds_is_no_answer_at_all(fault: str) -> None:
    async def ask(device: DeviceConnection, request: dict[str, Any], timeout: float) -> Any:
        events = (
            blocks(1, 501)
            if fault == "too_many_events"
            else [block(seq, "x" * 60_000) for seq in range(1, 21)]
        )
        return history_reply({"id": "x", "from": "gateway"}, events)

    assert asyncio.run(fetch_missing(ask, fake_device("d"), SESSION_ID, 0, 1.0, pages=5)) is None


# ---- what the gateway logs about it (A56 2.7) ----


@pytest.mark.asyncio
async def test_a_subscribe_the_device_served_is_logged_by_its_numbers_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=60)
    with caplog.at_level(logging.INFO, logger="rc_gateway.hub"):
        await subscribe(rig, since=50)
        request = await history_request(rig.device)
        assert request is not None
        await rig.hub.handle_device_frame(
            rig.device,
            history_reply(request, [block(seq, "secret words") for seq in range(51, 61)]),
        )
        await until(
            lambda: any(item.getMessage() == "session subscribed" for item in caplog.records)
        )

    (record,) = [item for item in caplog.records if item.getMessage() == "session subscribed"]
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["device_id"] == rig.device_id
    assert fields["session_id"] == SESSION_ID
    assert (fields["since"], fields["last_seq"], fields["events"]) == (50, 60, 10)
    assert fields["outcome"] == "device_backfilled"
    assert isinstance(fields["ms"], int)
    assert "secret words" not in str(fields)
    assert not [item for item in caplog.records if "secret words" in item.getMessage()]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_only_a_subscribe_that_needed_something_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    await put_session(rig, last_seq=60)
    with caplog.at_level(logging.INFO, logger="rc_gateway.hub"):
        await subscribe(rig, since=60, identifier="current")
        await subscribe(rig, identifier="cold")
    assert [item for item in caplog.records if item.getMessage() == "session subscribed"] == []
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_catch_up_after_a_hello_is_logged_by_its_numbers_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    with caplog.at_level(logging.INFO, logger="rc_gateway.catchup"):
        revived = await reconnect(rig, last_seq=60)
        request = await history_request(revived)
        assert request is not None
        await rig.hub.handle_device_frame(
            revived, history_reply(request, [block(seq, "secret words") for seq in range(51, 61)])
        )
        await until(lambda: len(watching.events()) == 10)
        await until(
            lambda: any(item.getMessage() == "session caught up" for item in caplog.records)
        )

    (record,) = [item for item in caplog.records if item.getMessage() == "session caught up"]
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["device_id"] == rig.device_id
    assert fields["session_id"] == SESSION_ID
    assert (fields["since"], fields["last_seq"], fields["events"]) == (50, 60, 10)
    assert fields["outcome"] == "device_backfilled"
    assert isinstance(fields["ms"], int)
    assert "secret words" not in str(fields)
    await rig.hub.stop()


def test_the_defaults_fit_the_limits_the_protocol_states() -> None:
    from rc_gateway.catchup import FETCH_BYTES, FETCH_EVENTS, FETCH_SECONDS, FETCHES_PER_DEVICE

    assert (FETCH_EVENTS, FETCH_BYTES, FETCH_SECONDS, FETCHES_PER_DEVICE) == (
        500,
        1024 * 1024,
        3.0,
        2,
    )
    assert FETCH_SECONDS < REQUEST_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_the_gate_keeps_nothing_for_a_session_or_device_nobody_waits_on() -> None:
    from rc_gateway.catchup import FetchGate

    gate = FetchGate()
    async with gate.slot("device-1", "session-1"):
        assert "session-1" in gate._sessions and "device-1" in gate._devices
    assert gate._sessions == {} and gate._devices == {}


def test_a_hold_keeps_events_in_order_and_refuses_what_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rc_gateway.catchup import CatchUp, Hold

    monkeypatch.setattr("rc_gateway.catchup.HOLD_EVENTS", 2)
    plan = CatchUp(session_id=SESSION_ID, owner="admin", after=0, tip=0, known=0, behind=())
    hold = Hold(fake_device("device-1"), plan)
    late, early = live(9), live(8)
    assert hold.add(late["event"], late)
    assert hold.add(early["event"], early)
    assert hold.add(block(10), live(10)) is False
    assert [event["seq"] for event, _ in hold.take()] == [8, 9]
    assert hold.take() == [] and hold.size == 0
