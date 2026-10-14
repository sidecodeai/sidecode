"""Amendment A22: the gateway reports the wheel it serves and drives an app-requested update."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.devices import UPDATE_FAILED, UPDATE_IDLE, UPDATE_RUNNING
from rc_gateway.hub import UPDATE_TIMEOUT_MESSAGE

from .conftest import (
    device_hello,
    drain_until,
    enroll_device,
    frames_of,
    hub_rig,
    write_wheel,
)

OTHER_BUILD = "9" * 64


def test_config_reports_the_served_wheel(
    tmp_path: Path, client: TestClient, auth: dict[str, str]
) -> None:
    build = write_wheel(tmp_path, version="0.4.0", body=b"a real wheel")
    body = client.get("/api/config", headers=auth).json()
    assert body["client"] == {
        "version": "0.4.0",
        "build": build,
        "url": "/dist/rc_client-latest.whl",
    }
    assert build == hashlib.sha256(b"a real wheel").hexdigest()
    # The build must be the file `/dist/rc_client-latest.whl` actually hands out.
    served = client.get("/dist/rc_client-latest.whl")
    assert hashlib.sha256(served.content).hexdigest() == build


def test_config_omits_the_client_without_a_wheel(client: TestClient, auth: dict[str, str]) -> None:
    """A source checkout that never built one has no build to name, so the field is absent."""
    body = client.get("/api/config", headers=auth).json()
    assert "client" not in body
    assert body["version"]


def test_hello_stores_the_build_it_reports(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    build = "a" * 64
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(client_build=build))
        device.receive_json()
        listed = client.get("/api/devices", headers=auth).json()["devices"][0]
        assert listed["client_build"] == build
        assert listed["update_state"] == UPDATE_IDLE
        assert listed["update_message"] is None

    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello())
        device.receive_json()
        listed = client.get("/api/devices", headers=auth).json()["devices"][0]
    # A client installed from source reports no build, and the stale one must not survive it.
    assert listed["client_build"] is None


@pytest.mark.asyncio
async def test_an_accepted_update_moves_the_device_through_updating(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, update_timeout=30.0)
    await _request_update(rig.hub, rig.app, rig.device_id, "req-1")
    frames_of(rig.device)
    frames_of(rig.app)

    await rig.hub.handle_device_frame(rig.device, _reply(rig.app.id, "req-1", ok=True))
    assert _last_device(frames_of(rig.app))["update_state"] == UPDATE_RUNNING

    await rig.hub.handle_device_hello(rig.device, device_hello(client_build="b" * 64))
    device = _last_device(frames_of(rig.app))
    assert device["update_state"] == UPDATE_IDLE
    assert device["client_build"] == "b" * 64
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_refused_update_leaves_the_device_alone(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, update_timeout=30.0)
    await _request_update(rig.hub, rig.app, rig.device_id, "req-2")
    frames_of(rig.app)

    await rig.hub.handle_device_frame(rig.device, _reply(rig.app.id, "req-2", ok=False))
    record = await rig.devices.get(rig.device_id)
    assert record is not None and record.update_state == UPDATE_IDLE
    assert not [frame for frame in frames_of(rig.app) if frame["type"] == "device.updated"]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_update_failed_reports_the_reason(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, update_timeout=30.0)
    await _request_update(rig.hub, rig.app, rig.device_id, "req-3")
    await rig.hub.handle_device_frame(rig.device, _reply(rig.app.id, "req-3", ok=True))
    frames_of(rig.app)

    await rig.hub.handle_device_frame(
        rig.device, {"type": "update.failed", "message": "the wheel's SHA-256 did not match"}
    )
    device = _last_device(frames_of(rig.app))
    assert device["update_state"] == UPDATE_FAILED
    assert device["update_message"] == "the wheel's SHA-256 did not match"
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_never_comes_back_fails_the_update(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, update_timeout=0.05)
    await _request_update(rig.hub, rig.app, rig.device_id, "req-4")
    await rig.hub.handle_device_frame(rig.device, _reply(rig.app.id, "req-4", ok=True))
    frames_of(rig.app)

    await asyncio.sleep(0.2)
    device = _last_device(frames_of(rig.app))
    assert device["update_state"] == UPDATE_FAILED
    assert device["update_message"] == UPDATE_TIMEOUT_MESSAGE
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_hello_cancels_the_timer(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, update_timeout=0.05)
    await _request_update(rig.hub, rig.app, rig.device_id, "req-5")
    await rig.hub.handle_device_frame(rig.device, _reply(rig.app.id, "req-5", ok=True))
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build="c" * 64))
    frames_of(rig.app)

    await asyncio.sleep(0.2)
    record = await rig.devices.get(rig.device_id)
    assert record is not None and record.update_state == UPDATE_IDLE
    assert not [frame for frame in frames_of(rig.app) if frame["type"] == "device.updated"]
    await rig.hub.stop()


def test_the_whole_update_path_runs_over_the_sockets(
    tmp_path: Path, client: TestClient, auth: dict[str, str]
) -> None:
    """The app asks, the device accepts, and the row says `updating` until the device comes back.

    This is the request an app still sends under A36, as Retry. The device says hello already on
    the served build so that nothing but the app asks it to update.
    """
    build = write_wheel(tmp_path, version="0.2.0", body=b"the new wheel")
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(client_build=build))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            assert client.get("/api/config", headers=auth).json()["client"]["build"] == build
            app.send_json(
                {
                    "type": "device.update",
                    "id": "e2e-1",
                    "device_id": enrolled["device_id"],
                    "build": build,
                }
            )
            forwarded = drain_until(device, "device.update")
            assert forwarded["build"] == build
            device.send_json(
                {
                    "type": "reply",
                    "id": forwarded["id"],
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {"accepted": True, "from": "d" * 64},
                }
            )
            updated = drain_until(app, "device.updated")
            assert updated["device"]["update_state"] == UPDATE_RUNNING
            device.send_json({"type": "update.failed", "message": "uv is not installed"})
            failed = drain_until(app, "device.updated")
    assert failed["device"]["update_state"] == UPDATE_FAILED
    assert failed["device"]["update_message"] == "uv is not installed"


def test_an_offline_device_cannot_be_updated(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        app.send_json(
            {
                "type": "device.update",
                "id": "req-6",
                "device_id": enrolled["device_id"],
                "build": OTHER_BUILD,
            }
        )
        reply = drain_until(app, "reply")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "device_offline"


async def _request_update(hub: Any, app: Any, device_id: str, identifier: str) -> None:
    await hub.handle_app_frame(
        app,
        {
            "type": "device.update",
            "id": identifier,
            "device_id": device_id,
            "build": OTHER_BUILD,
        },
    )


def _reply(target: str, identifier: str, *, ok: bool) -> dict[str, Any]:
    if ok:
        return {
            "type": "reply",
            "id": identifier,
            "from": target,
            "ok": True,
            "result": {"accepted": True, "from": None},
        }
    return {
        "type": "reply",
        "id": identifier,
        "from": target,
        "ok": False,
        "error": {"code": "conflict", "message": "1 session is running"},
    }


def _last_device(frames: list[dict[str, Any]]) -> dict[str, Any]:
    updates = [frame for frame in frames if frame["type"] == "device.updated"]
    assert updates, [frame["type"] for frame in frames]
    device: dict[str, Any] = updates[-1]["device"]
    return device
