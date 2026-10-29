"""Amendment A56: a restarted gateway holds its devices online while they reconnect.

A restart takes a few seconds, and every device reconnects on its own schedule. Without a grace
every device was reported offline for that long, and a request made in the meantime was refused.
An update earns a longer grace on a live link too, because the device's own restart comes on top.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.connections import AppConnection
from rc_gateway.devices import UPDATE_RUNNING, DeviceStore
from rc_gateway.frames import CLOSE_UNAUTHORIZED
from rc_gateway.hub import Hub

from .conftest import HubRig, fake_app, fake_device, frames_of, hub_rig, session_summary

SESSION_ID = "aaaa1111-2222-4333-8444-555555555555"


async def restarted(rig: HubRig, **options: Any) -> tuple[Hub, AppConnection]:
    """The gateway after a clean restart: a new hub on the stores the first one left behind."""
    await rig.hub.stop()
    hub = Hub(rig.index, rig.devices, **options)
    app = fake_app()
    await hub.attach_app(app)
    await hub.start()
    return hub, app


async def until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


# ---- the record a clean stop leaves ----


@pytest.mark.asyncio
async def test_a_device_online_at_the_stop_is_online_after_the_restart(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)

    hub, app = await restarted(rig, restart_grace=5.0)

    assert hub.device_online(rig.device_id) is True
    assert frames_of(app) == []
    await hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_does_not_come_back_is_reported_offline_when_the_grace_ends(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path)
    hub, app = await restarted(rig, restart_grace=0.15)
    assert hub.device_online(rig.device_id) is True

    await until(lambda: not hub.device_online(rig.device_id))

    await until(lambda: not app.queue.empty())
    (announced,) = frames_of(app)
    assert announced["type"] == "device.updated"
    assert announced["device"]["online"] is False
    await hub.stop()


@pytest.mark.asyncio
async def test_a_reconnect_inside_the_restart_grace_says_nothing_to_the_apps(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path)
    hub, app = await restarted(rig, restart_grace=0.3)

    await hub.attach_device(fake_device(rig.device_id))
    await asyncio.sleep(0.5)

    assert hub.device_online(rig.device_id) is True
    assert frames_of(app) == []
    await hub.stop()


@pytest.mark.asyncio
async def test_a_request_made_during_the_restart_grace_reaches_the_device_that_returns(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    hub, app = await restarted(rig, restart_grace=5.0)

    sent = asyncio.ensure_future(
        hub.handle_app_frame(
            app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
        )
    )
    await asyncio.sleep(0.05)
    assert not sent.done()

    returned = fake_device(rig.device_id)
    await hub.attach_device(returned)
    await sent

    assert [frame["id"] for frame in frames_of(returned)] == ["r1"]
    assert [frame.get("error") for frame in frames_of(app)] == []
    await hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_was_already_offline_at_the_stop_is_not_held(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)
    # What a revoked device looks like: gone at once, with no grace to be held in.
    await rig.device.stop(code=CLOSE_UNAUTHORIZED, reason="device revoked")
    await rig.hub.detach_device(rig.device)

    hub, _ = await restarted(rig, restart_grace=5.0)

    assert hub.device_online(rig.device_id) is False
    await hub.stop()


@pytest.mark.asyncio
async def test_a_device_in_its_own_grace_at_the_stop_is_held_too(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.detach_device(rig.device)
    assert rig.hub.device_online(rig.device_id) is True

    hub, _ = await restarted(rig, restart_grace=5.0)

    assert hub.device_online(rig.device_id) is True
    await hub.stop()


@pytest.mark.asyncio
async def test_a_crash_leaves_nothing_to_hold(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)
    # No stop: the process was killed, so no record was made and the new one starts cold.
    hub = Hub(rig.index, rig.devices, restart_grace=5.0)
    await hub.start()

    assert hub.device_online(rig.device_id) is False
    await hub.stop()
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_start_that_cannot_read_the_record_is_a_cold_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.stop()

    async def unreadable(**_: Any) -> list[str]:
        raise OSError("the disk went away")

    monkeypatch.setattr(rig.devices, "take_stopped", unreadable)
    hub = Hub(rig.index, rig.devices, restart_grace=5.0)
    await hub.start()

    assert hub.device_online(rig.device_id) is False
    await hub.stop()


@pytest.mark.asyncio
async def test_the_record_is_spent_by_the_start_that_reads_it(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)
    hub, _ = await restarted(rig, restart_grace=5.0)
    # A process that crashes now must not find its predecessor's record.
    assert await rig.devices.take_stopped(within=120) == []
    await hub.stop()


@pytest.mark.asyncio
async def test_a_record_older_than_the_window_is_ignored(tmp_path: Path) -> None:
    devices = DeviceStore(tmp_path / "devices.sqlite3")
    grant = await devices.create_pairing("admin")
    enrolled = await devices.redeem(
        grant.code,
        name="mac",
        platform="macos",
        hostname="mac.local",
        arch="arm64",
        client_version="0.1.0",
    )
    assert not isinstance(enrolled, str)

    await devices.record_stop([enrolled.device_id], now=1_000)
    assert await devices.take_stopped(within=120, now=1_121) == []

    await devices.record_stop([enrolled.device_id], now=1_000)
    assert await devices.take_stopped(within=120, now=1_120) == [enrolled.device_id]


@pytest.mark.asyncio
async def test_a_device_removed_since_the_stop_is_not_returned(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.stop()
    assert await rig.devices.revoke(rig.device_id, "admin")

    assert await rig.devices.take_stopped(within=120) == []


@pytest.mark.asyncio
async def test_a_device_that_was_updating_gets_the_longer_grace(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path)
    await rig.devices.set_update(rig.device_id, UPDATE_RUNNING)

    hub, _ = await restarted(rig, restart_grace=0.1, update_grace=1.2)
    await asyncio.sleep(0.4)

    assert hub.device_online(rig.device_id) is True
    await until(lambda: not hub.device_online(rig.device_id), timeout=3.0)
    await hub.stop()


# ---- the grace a running update earns on a live link ----


async def accepted_update(rig: HubRig) -> None:
    await rig.hub.handle_app_frame(
        rig.app,
        {"type": "device.update", "id": "u1", "device_id": rig.device_id, "build": "b" * 64},
    )
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "reply", "id": "u1", "from": rig.app.id, "ok": True, "result": {"accepted": True}},
    )


@pytest.mark.asyncio
async def test_a_device_that_accepted_an_update_is_waited_for_longer(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=0.1, update_grace=1.2, update_timeout=30.0)
    await accepted_update(rig)

    await rig.hub.detach_device(rig.device)
    await asyncio.sleep(0.4)
    assert rig.hub.device_online(rig.device_id) is True

    await until(lambda: not rig.hub.device_online(rig.device_id), timeout=3.0)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_is_not_updating_gets_the_short_grace(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=0.1, update_grace=0.6, update_timeout=30.0)

    await rig.hub.detach_device(rig.device)
    await asyncio.sleep(0.3)

    assert rig.hub.device_online(rig.device_id) is False
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_request_waits_out_the_longer_grace_of_an_update(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=0.1, update_grace=2.0, update_timeout=30.0)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    await accepted_update(rig)
    frames_of(rig.app)
    await rig.hub.detach_device(rig.device)

    sent = asyncio.ensure_future(
        rig.hub.handle_app_frame(
            rig.app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
        )
    )
    await asyncio.sleep(0.4)
    assert not sent.done()

    returned = fake_device(rig.device_id)
    await rig.hub.attach_device(returned)
    await sent
    assert [frame["id"] for frame in frames_of(returned)] == ["r1"]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_the_grace_is_short_again_once_the_updated_device_has_come_back(
    tmp_path: Path,
) -> None:
    from .conftest import device_hello

    rig = await hub_rig(tmp_path, offline_grace=0.1, update_grace=0.6, update_timeout=30.0)
    await accepted_update(rig)
    await rig.hub.detach_device(rig.device)
    returned = fake_device(rig.device_id)
    await rig.hub.attach_device(returned)
    await rig.hub.handle_device_hello(returned, device_hello(client_build="b" * 64))

    await rig.hub.detach_device(returned)
    await asyncio.sleep(0.3)

    assert rig.hub.device_online(rig.device_id) is False
    await rig.hub.stop()
