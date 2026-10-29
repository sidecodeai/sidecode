"""Amendment A56: a device's hello fetches for that device's sessions and for nobody else's.

The hold on a session while its events are fetched belongs to the device that owns the session.
Another device's hello, of the same account or of another, may neither take it over nor publish
what it held through its own connection, which would stamp the events with its `device_id` and,
for a `resume` event, raise the push notice for the wrong owner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.catchup import CatchUp, Hold
from rc_gateway.connections import DeviceConnection
from rc_gateway.hub import SessionResumeNotice

from .catch_up_support import (
    OTHER_ID,
    PATIENT,
    SESSION_ID,
    Watching,
    block,
    blocks,
    history_reply,
    history_request,
    live,
    put_session,
    reconnect,
    subscribe,
    until,
)
from .conftest import HubRig, another_device, device_hello, hub_rig, session_summary


def _stamps(watching: Watching) -> set[tuple[str, str]]:
    """Which device each session's events were published under."""
    return {
        (frame["session_id"], frame["device_id"])
        for frame in watching.read()
        if frame["type"] == "session.event"
    }


@pytest.mark.asyncio
async def test_a_hello_leaves_the_fetches_of_the_accounts_other_devices_alone(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    other = await another_device(rig)
    await put_session(rig, last_seq=50)
    await put_session(rig, last_seq=50, session_id=OTHER_ID, device=other)
    await subscribe(rig, since=50)
    await subscribe(rig, since=50, session_id=OTHER_ID, identifier="s2")
    first = await reconnect(rig, last_seq=60)
    first_request = await history_request(first)
    assert first_request is not None
    await rig.hub.handle_device_frame(first, live(61))

    # The account's other device says hello while the first one's fetch is in flight.
    await rig.hub.handle_device_hello(
        other, device_hello(sessions=[session_summary(OTHER_ID, other.device_id, last_seq=60)])
    )
    other_request = await history_request(other)

    assert other_request is not None
    assert rig.hub._catchups.held(SESSION_ID) is not None
    assert watching.events() == []
    await rig.hub.handle_device_frame(first, history_reply(first_request, blocks(51, 60)))
    await rig.hub.handle_device_frame(other, history_reply(other_request, blocks(51, 60)))
    await until(lambda: len(watching.events()) == 11 and len(watching.events(OTHER_ID)) == 10)
    assert watching.events() == list(range(51, 62))
    assert watching.events(OTHER_ID) == list(range(51, 61))
    assert _stamps(watching) == {(SESSION_ID, rig.device_id), (OTHER_ID, other.device_id)}
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hello_of_another_account_cannot_have_what_a_fetch_held_published_as_its_own(
    tmp_path: Path,
) -> None:
    announced: list[SessionResumeNotice] = []

    async def on_resume(notice: SessionResumeNotice) -> None:
        announced.append(notice)

    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT, on_session_resume=on_resume)
    watching = Watching(rig.app)
    stranger = await another_device(rig, account="mallory")
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None
    resume = {"seq": 61, "ts": 61, "kind": "resume", "status": "scheduled", "at": 1}
    await rig.hub.handle_device_frame(
        revived, {"type": "session.event", "session_id": SESSION_ID, "event": resume}
    )

    await rig.hub.handle_device_hello(stranger, device_hello(sessions=[]))
    await asyncio.sleep(0.05)

    assert announced == []
    assert watching.events() == []
    assert rig.hub._catchups.held(SESSION_ID) is not None
    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))
    await until(lambda: len(watching.events()) == 11 and len(announced) == 1)
    (notice,) = announced
    assert (notice.owner, notice.device_id, notice.session_id) == (
        "admin",
        rig.device_id,
        SESSION_ID,
    )
    assert _stamps(watching) == {(SESSION_ID, rig.device_id)}
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_fetch_is_published_after_its_sessions_owner_left_the_cache(tmp_path: Path) -> None:
    """A restart brings every device back at once, and the map of owners is a bounded cache."""
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    revived = await reconnect(rig, last_seq=60)
    request = await history_request(revived)
    assert request is not None
    await rig.hub.handle_device_frame(revived, live(61))
    del rig.hub._owners[SESSION_ID]

    await rig.hub.handle_device_frame(revived, history_reply(request, blocks(51, 60)))

    await until(lambda: len(watching.events()) == 11)
    assert watching.events() == list(range(51, 62))
    await rig.hub.stop()


def _hold_of(rig: HubRig, device: DeviceConnection) -> tuple[CatchUp, Hold]:
    """A hold on the rig's session in the name of a device, which may not be its owner."""
    item = CatchUp(
        session_id=SESSION_ID, owner="mallory", after=50, tip=60, known=50, behind=(rig.app,)
    )
    hold = Hold(device, item)
    rig.hub._catchups._holds[SESSION_ID] = hold
    return item, hold


def _frame_of(event: dict[str, Any]) -> dict[str, Any]:
    return {"type": "session.event", "session_id": SESSION_ID, "event": event}


async def _rig_with_a_stranger(tmp_path: Path) -> tuple[HubRig, Watching, DeviceConnection]:
    rig = await hub_rig(tmp_path, fetch_timeout=PATIENT)
    watching = Watching(rig.app)
    stranger = await another_device(rig, account="mallory")
    await put_session(rig, last_seq=50)
    await subscribe(rig, since=50)
    return rig, watching, stranger


@pytest.mark.asyncio
async def test_a_fetch_is_not_applied_through_a_device_that_does_not_own_the_session(
    tmp_path: Path,
) -> None:
    rig, watching, stranger = await _rig_with_a_stranger(tmp_path)
    item, hold = _hold_of(rig, stranger)

    applied = await rig.hub._catchups._apply(stranger, item, hold, blocks(51, 60))

    assert applied is False
    assert watching.events() == []
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hold_is_not_released_through_a_device_that_does_not_own_the_session(
    tmp_path: Path,
) -> None:
    rig, watching, stranger = await _rig_with_a_stranger(tmp_path)
    _, hold = _hold_of(rig, stranger)
    event = block(61, "private words")
    assert hold.add(event, _frame_of(event))

    await rig.hub._catchups.release(SESSION_ID, hold)

    assert watching.events() == []
    assert rig.hub._catchups.held(SESSION_ID) is None
    await rig.hub.stop()
