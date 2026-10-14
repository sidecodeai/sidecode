"""Amendment A36: the gateway brings every device to the wheel it serves, without being asked."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.connections import Connection
from rc_gateway.devices import UPDATE_FAILED, UPDATE_IDLE, UPDATE_RUNNING
from rc_gateway.hub import UPDATE_TIMEOUT_MESSAGE

from .conftest import (
    HubRig,
    device_hello,
    drain_until,
    enroll_device,
    frames_of,
    hub_rig,
    session_summary,
    write_wheel,
)

SERVED_BUILD = "a" * 64
OLD_BUILD = "b" * 64
NEXT_BUILD = "c" * 64
SESSION_ID = "dddd1111-2222-4333-8444-555555555555"

#: Long enough for the store reads the policy makes on its way to the device, short enough that a
#: test asserting nothing was asked does not sit around.
SETTLE_SECONDS = 0.3


class Served:
    """The build the gateway serves, which a test can change as a deployment would."""

    def __init__(self, build: str | None) -> None:
        self.build = build

    def __call__(self) -> str | None:
        return self.build


async def rig_with_wheel(
    tmp_path: Path,
    *,
    build: str | None = SERVED_BUILD,
    update_retry: float = 30.0,
    update_timeout: float = 30.0,
) -> tuple[HubRig, Served]:
    served = Served(build)
    rig = await hub_rig(
        tmp_path,
        update_timeout=update_timeout,
        update_retry=update_retry,
        served_build=served,
    )
    return rig, served


@pytest.mark.asyncio
async def test_a_device_behind_the_served_wheel_is_asked_on_its_own(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))

    ask = await next_ask(rig.device)
    assert ask is not None
    assert ask["build"] == SERVED_BUILD
    assert ask["from"] == "gateway"
    assert ask["device_id"] == rig.device_id
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_accepted_automatic_update_shows_as_updating(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None
    frames_of(rig.app)

    await rig.hub.handle_device_frame(rig.device, accepted(ask))
    announced = await next_frame(rig.app, "device.updated")
    assert announced is not None
    assert announced["device"]["update_state"] == UPDATE_RUNNING
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_busy_device_is_asked_again_when_its_sessions_go_quiet(tmp_path: Path) -> None:
    """The retry period is far away: the quiet moment is what brings the second request."""
    rig, _ = await rig_with_wheel(tmp_path, update_retry=30.0)
    await rig.hub.handle_device_hello(
        rig.device, device_hello(client_build=OLD_BUILD, sessions=[running_session(rig.device_id)])
    )
    ask = await next_ask(rig.device)
    assert ask is not None

    await rig.hub.handle_device_frame(rig.device, refused(ask, "1 session is running"))
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None

    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    again = await next_ask(rig.device)
    assert again is not None and again["build"] == SERVED_BUILD
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_stays_busy_is_asked_again_when_the_period_is_up(
    tmp_path: Path,
) -> None:
    rig, _ = await rig_with_wheel(tmp_path, update_retry=0.05)
    await rig.hub.handle_device_hello(
        rig.device, device_hello(client_build=OLD_BUILD, sessions=[running_session(rig.device_id)])
    )
    ask = await next_ask(rig.device)
    assert ask is not None

    await rig.hub.handle_device_frame(rig.device, refused(ask, "1 session is running"))
    # Nothing says the session ended; only the period passing asks again.
    assert await next_ask(rig.device) is not None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_wheel_deployed_while_a_device_was_busy_is_the_one_asked_for(
    tmp_path: Path,
) -> None:
    rig, served = await rig_with_wheel(tmp_path, update_retry=0.05)
    await rig.hub.handle_device_hello(
        rig.device, device_hello(client_build=OLD_BUILD, sessions=[running_session(rig.device_id)])
    )
    ask = await next_ask(rig.device)
    assert ask is not None and ask["build"] == SERVED_BUILD

    await rig.hub.handle_device_frame(rig.device, refused(ask, "1 session is running"))
    served.build = NEXT_BUILD
    again = await next_ask(rig.device)
    assert again is not None and again["build"] == NEXT_BUILD
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_session_still_running_does_not_release_the_retry(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path, update_retry=30.0)
    await rig.hub.handle_device_hello(
        rig.device, device_hello(client_build=OLD_BUILD, sessions=[running_session(rig.device_id)])
    )
    ask = await next_ask(rig.device)
    assert ask is not None

    await rig.hub.handle_device_frame(rig.device, refused(ask, "1 session is running"))
    await rig.hub.handle_device_frame(
        rig.device,
        {
            "type": "session.updated",
            "session": session_summary(SESSION_ID, rig.device_id, state="running"),
        },
    )
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_client_that_cannot_update_itself_is_asked_once(tmp_path: Path) -> None:
    """A client installed from source answers `unsupported`, and is left alone (A36)."""
    rig, _ = await rig_with_wheel(tmp_path, update_retry=0.05)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None

    await rig.hub.handle_device_frame(
        rig.device, refused(ask, "installed from source", code="unsupported")
    )
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    # Not even another hello on the same connection asks again.
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_already_runs_the_build_is_not_asked_again(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path, update_retry=0.05)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None

    await rig.hub.handle_device_frame(rig.device, refused(ask, "already on this build"))
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_failed_update_is_remembered_and_never_retried_by_the_gateway(
    tmp_path: Path,
) -> None:
    rig, _ = await rig_with_wheel(tmp_path)
    await drive_failure(rig, "the wheel's SHA-256 did not match")
    record = await rig.devices.get(rig.device_id)
    assert record is not None
    assert record.update_state == UPDATE_FAILED
    assert record.update_failed_build == SERVED_BUILD

    # The old client comes back: the failure is what a person has to see, so it survives the
    # hello that A22 used to clear, and nothing is asked again.
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    restored = await rig.devices.get(rig.device_id)
    assert restored is not None
    assert restored.update_state == UPDATE_FAILED
    assert restored.update_message == "the wheel's SHA-256 did not match"
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_moved_to_another_build_takes_the_failure_with_it(
    tmp_path: Path,
) -> None:
    """Somebody re-ran the installer: the machine is not where it failed, so nothing says it is."""
    rig, _ = await rig_with_wheel(tmp_path)
    await drive_failure(rig, "uv is not installed")

    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=NEXT_BUILD))
    record = await rig.devices.get(rig.device_id)
    assert record is not None
    assert record.update_state == UPDATE_IDLE
    assert record.update_message is None
    assert record.update_failed_build is None
    assert record.client_build == NEXT_BUILD
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_that_never_comes_back_remembers_the_build_it_failed_on(
    tmp_path: Path,
) -> None:
    rig, _ = await rig_with_wheel(tmp_path, update_timeout=0.05)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None
    await rig.hub.handle_device_frame(rig.device, accepted(ask))

    await asyncio.sleep(0.2)
    record = await rig.devices.get(rig.device_id)
    assert record is not None
    assert record.update_state == UPDATE_FAILED
    assert record.update_message == UPDATE_TIMEOUT_MESSAGE
    assert record.update_failed_build == SERVED_BUILD
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_app_retry_is_forwarded_and_clears_the_memory(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path)
    await drive_failure(rig, "uv is not installed")
    frames_of(rig.device)

    await rig.hub.handle_app_frame(
        rig.app,
        {
            "type": "device.update",
            "id": "retry-1",
            "device_id": rig.device_id,
            "build": SERVED_BUILD,
        },
    )
    forwarded = await next_ask(rig.device)
    assert forwarded is not None
    assert forwarded["from"] == rig.app.id
    assert forwarded["id"] == "retry-1"

    await rig.hub.handle_device_frame(rig.device, accepted(forwarded))
    record = await rig.devices.get(rig.device_id)
    assert record is not None
    assert record.update_state == UPDATE_RUNNING
    assert record.update_failed_build is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_newer_wheel_is_asked_for_although_the_last_one_failed(tmp_path: Path) -> None:
    rig, served = await rig_with_wheel(tmp_path)
    await drive_failure(rig, "the installer exited 1")

    served.build = NEXT_BUILD
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None and ask["build"] == NEXT_BUILD
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_client_with_no_build_of_its_own_is_asked_nothing(tmp_path: Path) -> None:
    """`client_build` is null on a client installed from source: there is nothing to compare."""
    rig, _ = await rig_with_wheel(tmp_path)
    await rig.hub.handle_device_hello(rig.device, device_hello())
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_gateway_with_no_wheel_asks_nothing(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path, build=None)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_device_already_on_the_served_build_is_asked_nothing(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=SERVED_BUILD))
    assert await next_ask(rig.device, timeout=SETTLE_SECONDS) is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_update_that_works_leaves_the_row_idle(tmp_path: Path) -> None:
    rig, _ = await rig_with_wheel(tmp_path)
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None
    await rig.hub.handle_device_frame(rig.device, accepted(ask))

    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=SERVED_BUILD))
    record = await rig.devices.get(rig.device_id)
    assert record is not None
    assert record.update_state == UPDATE_IDLE
    assert record.client_build == SERVED_BUILD
    assert record.update_failed_build is None
    await rig.hub.stop()


def test_the_automatic_update_runs_over_the_sockets(
    tmp_path: Path, client: TestClient, auth: dict[str, str]
) -> None:
    """Nobody presses anything: the device says hello, and the gateway asks for the wheel."""
    build = write_wheel(tmp_path, version="1.4.6", body=b"the newest wheel")
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello(client_build=OLD_BUILD))
            asked = drain_until(device, "device.update")
            assert asked["build"] == build
            assert asked["from"] == "gateway"
            device.send_json(
                {
                    "type": "reply",
                    "id": asked["id"],
                    "from": asked["from"],
                    "ok": True,
                    "result": {"accepted": True, "from": OLD_BUILD},
                }
            )
            updated = drain_until_update(app, UPDATE_RUNNING)
    assert updated["update_state"] == UPDATE_RUNNING


async def drive_failure(rig: HubRig, message: str) -> None:
    """Take one device through an automatic update that the device reports as failed."""
    await rig.hub.handle_device_hello(rig.device, device_hello(client_build=OLD_BUILD))
    ask = await next_ask(rig.device)
    assert ask is not None
    await rig.hub.handle_device_frame(rig.device, accepted(ask))
    assert await wait_for_state(rig, UPDATE_RUNNING) == UPDATE_RUNNING
    await rig.hub.handle_device_frame(rig.device, {"type": "update.failed", "message": message})


async def next_ask(connection: Connection, *, timeout: float = 2.0) -> dict[str, Any] | None:
    """The next ``device.update`` queued for a device, or None if none arrives in time."""
    return await next_frame(connection, "device.update", timeout=timeout)


async def next_frame(
    connection: Connection, kind: str, *, timeout: float = 2.0
) -> dict[str, Any] | None:
    """The next frame of ``kind`` queued for a connection, or None if none arrives in time.

    The policy runs as its own task and reads the device row across a thread before it asks, so a
    test cannot assume anything is queued by the time the call it made has returned.
    """
    deadline = time.monotonic() + timeout
    while True:
        for frame in frames_of(connection):
            if frame.get("type") == kind:
                return frame
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.01)


async def wait_for_state(rig: HubRig, state: str, *, timeout: float = 2.0) -> str:
    """Poll the stored device row until it reaches ``state``, and report what it is either way."""
    deadline = time.monotonic() + timeout
    while True:
        record = await rig.devices.get(rig.device_id)
        current = "" if record is None else record.update_state
        if current == state or time.monotonic() >= deadline:
            return current
        await asyncio.sleep(0.01)


def accepted(ask: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "reply",
        "id": ask["id"],
        "from": ask["from"],
        "ok": True,
        "result": {"accepted": True, "from": OLD_BUILD},
    }


def refused(ask: dict[str, Any], message: str, *, code: str = "conflict") -> dict[str, Any]:
    return {
        "type": "reply",
        "id": ask["id"],
        "from": ask["from"],
        "ok": False,
        "error": {"code": code, "message": message},
    }


def running_session(device_id: str) -> dict[str, Any]:
    return session_summary(SESSION_ID, device_id, state="running")


def drain_until_update(app: Any, state: str, limit: int = 40) -> dict[str, Any]:
    """Read app frames until a device is announced in ``state``.

    The device coming online is announced first and says nothing about an update, so a plain
    "the next `device.updated`" would read the wrong frame.
    """
    seen: list[str] = []
    for _ in range(limit):
        frame = app.receive_json()
        seen.append(str(frame.get("type")))
        if frame.get("type") == "device.updated" and frame["device"]["update_state"] == state:
            device: dict[str, Any] = frame["device"]
            return device
    raise AssertionError(f"no device reached {state!r}; saw {seen}")
