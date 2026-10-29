"""The `hello_ack` is the first frame a device reads, whoever was waiting to write to it (A56)."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from rc_gateway.connections import SlowClientError
from rc_gateway.state import GatewayState

from .conftest import (
    device_hello,
    drain_until,
    enroll_device,
    fake_device,
    frames_of,
    session_summary,
)


@pytest.mark.asyncio
async def test_frames_sent_during_the_handshake_follow_the_greeting() -> None:
    device = fake_device("device-1")
    device.hold_until_greeted()

    await device.send({"type": "ping"})
    await device.send({"type": "session.stop", "id": "r1"})
    assert frames_of(device) == []

    await device.greet({"type": "hello_ack"}, {"type": "preferences"})
    assert [frame["type"] for frame in frames_of(device)] == [
        "hello_ack",
        "preferences",
        "ping",
        "session.stop",
    ]

    await device.send({"type": "session.send", "id": "r2"})
    assert [frame["type"] for frame in frames_of(device)] == ["session.send"]


@pytest.mark.asyncio
async def test_a_connection_that_closes_mid_handshake_gives_back_what_it_held() -> None:
    device = fake_device("device-1")
    device.hold_until_greeted()
    released: list[str] = []

    await device.send({"type": "session.send"}, release=lambda: released.append("held"))
    assert released == []

    await device.stop()
    assert released == ["held"]
    with pytest.raises(ConnectionError):
        await device.send({"type": "ping"})


@pytest.mark.asyncio
async def test_what_waits_for_the_greeting_is_bounded_like_the_queue() -> None:
    device = fake_device("device-1")
    device.hold_until_greeted()

    for _ in range(device.cap):
        await device.send({"type": "ping"})
    released: list[str] = []
    with pytest.raises(SlowClientError):
        await device.send({"type": "ping"}, release=lambda: released.append("refused"))
    assert released == ["refused"]
    await device.stop()


def test_a_request_parked_for_a_reconnecting_device_follows_the_ack(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    # A request waits out a link that dropped (A13) and is released the moment the device is
    # attached; that must not put it ahead of the ack the device is waiting for.
    state.hub.offline_grace = 5.0
    state.hub.request_timeout = 30.0
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    session_id = str(uuid.uuid4())
    # Enough summaries that applying the hello takes several awaits, which is where a parked
    # request could get in ahead of the ack.
    sessions = [session_summary(session_id, enrolled["device_id"])] + [
        session_summary(str(uuid.uuid4()), enrolled["device_id"]) for _ in range(60)
    ]
    order: list[str] = []
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello(sessions=sessions))
            assert device.receive_json()["type"] == "hello_ack"
        deadline = time.time() + 3
        while time.time() < deadline and enrolled["device_id"] not in state.hub._grace:
            time.sleep(0.01)

        def reconnect() -> None:
            time.sleep(0.3)  # the app's request is parked by then
            with client.websocket_connect("/ws/device", headers=headers) as device:
                device.send_json(device_hello(sessions=sessions))
                while "session.history" not in order:
                    order.append(device.receive_json()["type"])
                device.send_json(
                    {"type": "reply", "id": "req-1", "from": "x", "ok": True, "result": {}}
                )

        thread = threading.Thread(target=reconnect)
        thread.start()
        app.send_json(
            {"type": "session.history", "id": "req-1", "session_id": session_id, "limit": 200}
        )
        thread.join(timeout=10)
    assert order[:3] == ["hello_ack", "preferences", "session.history"]
