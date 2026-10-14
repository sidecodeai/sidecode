"""Request forwarding, reply routing, offline and timeout replies, session fan-out."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rc_gateway.state import GatewayState

from .conftest import (
    close_code_for,
    device_hello,
    drain_until,
    enroll_device,
    fake_app,
    frames_of,
    hub_rig,
    session_summary,
)

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _device_headers(enrolled: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {enrolled['device_token']}"}


def test_app_hello_carries_devices_and_sessions(client: TestClient, auth: dict[str, str]) -> None:
    enroll_device(client, auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        hello = app.receive_json()
    assert hello["type"] == "hello"
    assert hello["protocol"] == 1
    assert hello["user"] == {"username": "admin", "role": "admin"}
    assert len(hello["devices"]) == 1
    assert hello["sessions"] == []
    assert hello["stt"] == {"enabled": True, "languages": ["auto"]}
    assert hello["server_time"] > 0


def test_app_socket_requires_a_credential(client: TestClient) -> None:
    assert close_code_for(client, "/ws/app") == 4401


def test_request_is_forwarded_with_from_and_the_reply_comes_back(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        assert device.receive_json()["type"] == "hello_ack"
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(
                {"type": "session.send", "id": "req-1", "session_id": SESSION_ID, "text": "hi"}
            )
            forwarded = drain_until(device, "session.send")
            assert forwarded["id"] == "req-1"
            assert forwarded["device_id"] == enrolled["device_id"]
            assert forwarded["from"]
            assert forwarded["text"] == "hi"

            device.send_json(
                {
                    "type": "reply",
                    "id": "req-1",
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {"accepted": "sent"},
                }
            )
            reply = drain_until(app, "reply")
            assert reply["ok"] is True
            assert reply["result"] == {"accepted": "sent"}
            assert "from" not in reply


def test_a_request_addressed_by_device_id_is_forwarded_unchanged(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello())
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(
                {
                    "type": "session.create",
                    "id": "req-2",
                    "device_id": enrolled["device_id"],
                    "agent": "claude",
                    "cwd": "/tmp",
                }
            )
            forwarded = drain_until(device, "session.create")
            assert forwarded["agent"] == "claude"
            assert forwarded["cwd"] == "/tmp"


def test_device_mkdir_is_forwarded_to_the_device_it_names(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A37: the picker's New folder travels like device.dirs, by device_id, with from stamped."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello())
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(
                {
                    "type": "device.mkdir",
                    "id": "req-mkdir",
                    "device_id": enrolled["device_id"],
                    "path": "/Users/me/dev",
                    "name": "new-project",
                }
            )
            forwarded = drain_until(device, "device.mkdir")
            assert forwarded["path"] == "/Users/me/dev"
            assert forwarded["name"] == "new-project"
            assert forwarded["from"]
            device.send_json(
                {
                    "type": "reply",
                    "id": "req-mkdir",
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {
                        "path": "/Users/me/dev/new-project",
                        "parent": "/Users/me/dev",
                        "entries": [],
                        "recent": [],
                    },
                }
            )
            reply = drain_until(app, "reply")
            assert reply["ok"] is True
            assert reply["result"]["path"] == "/Users/me/dev/new-project"


def test_offline_device_yields_a_device_offline_reply(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        app.send_json(
            {
                "type": "device.agents",
                "id": "req-3",
                "device_id": enrolled["device_id"],
            }
        )
        reply = drain_until(app, "reply")
        assert reply["ok"] is False
        assert reply["error"]["code"] == "device_offline"


def test_unknown_session_yields_not_found(client: TestClient, auth: dict[str, str]) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        app.send_json({"type": "session.stop", "id": "req-4", "session_id": "nope"})
        reply = drain_until(app, "reply")
        assert reply["error"]["code"] == "not_found"


def test_a_silent_device_produces_a_timeout_reply(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.stop", "id": "req-5", "session_id": SESSION_ID})
            drain_until(device, "session.stop")
            reply = drain_until(app, "reply")
            assert reply["id"] == "req-5"
            assert reply["error"]["code"] == "timeout"


def test_pending_requests_fail_when_the_device_drops(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
            device.send_json(
                device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
            )
            device.receive_json()
            drain_until(app, "device.updated")
            app.send_json({"type": "session.stop", "id": "req-6", "session_id": SESSION_ID})
            drain_until(device, "session.stop")
        reply = drain_until(app, "reply")
        assert reply["id"] == "req-6"
        assert reply["error"]["code"] == "device_offline"


def test_a_multi_megabyte_attachment_forwards_intact(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A session.send may carry attachments, so the device queue must not treat it as a flood."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            blob = "A" * (4 * 1024 * 1024)
            app.send_json(
                {
                    "type": "session.send",
                    "id": "req-big",
                    "session_id": SESSION_ID,
                    "text": "look at this",
                    "attachments": [{"name": "shot.png", "mime": "image/png", "data_base64": blob}],
                    "mode": "auto",
                }
            )
            forwarded = drain_until(device, "session.send")
            assert forwarded["attachments"][0]["data_base64"] == blob
            assert forwarded["attachments"][0]["name"] == "shot.png"


def test_an_unsupported_app_request_is_rejected(client: TestClient, auth: dict[str, str]) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        app.send_json({"type": "session.nonsense", "id": "req-7"})
        reply = drain_until(app, "reply")
        assert reply["error"]["code"] == "bad_request"


def test_session_updates_reach_every_app(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello())
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            device.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_ID, enrolled["device_id"], state="running"),
                }
            )
            pushed = drain_until(app, "session.updated")
            assert pushed["session"]["session_id"] == SESSION_ID
            assert pushed["session"]["state"] == "running"

            device.send_json({"type": "session.removed", "session_id": SESSION_ID})
            removed = drain_until(app, "session.removed")
            assert removed == {
                "type": "session.removed",
                "session_id": SESSION_ID,
                "device_id": enrolled["device_id"],
            }


def test_pairing_progress_reaches_enrolled_online_and_agents(
    client: TestClient, auth: dict[str, str]
) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        enrolled = enroll_device(client, auth)
        waiting = drain_until(app, "pairing.progress")
        assert waiting["step"] == "waiting"
        steps = [waiting["step"]]
        headers = _device_headers(enrolled)
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            for _ in range(8):
                frame = app.receive_json()
                if frame["type"] == "pairing.progress":
                    steps.append(frame["step"])
                    if frame["step"] == "agents":
                        assert frame["device"]["device_id"] == enrolled["device_id"]
                        break
    assert steps == ["waiting", "enrolled", "online", "agents"]


def test_device_state_survives_a_gateway_restart(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
    listed = client.get("/api/sessions", headers=auth).json()["sessions"]
    assert [item["session_id"] for item in listed] == [SESSION_ID]
    assert listed[0]["device_id"] == enrolled["device_id"]
    filtered = client.get("/api/sessions", params={"device_id": "other"}, headers=auth).json()[
        "sessions"
    ]
    assert filtered == []


def test_attachments_beyond_the_protocol_bounds_are_refused(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Section 5 caps attachments; the gateway enforces it so devices never have to."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(
                {
                    "type": "session.send",
                    "id": "many",
                    "session_id": SESSION_ID,
                    "text": "hi",
                    "attachments": [
                        {"name": f"{index}.png", "mime": "image/png", "data_base64": "AAAA"}
                        for index in range(9)
                    ],
                }
            )
            reply = drain_until(app, "reply")
            assert reply["error"]["code"] == "too_large"

            app.send_json(
                {
                    "type": "session.send",
                    "id": "big",
                    "session_id": SESSION_ID,
                    "text": "hi",
                    "attachments": [
                        {"name": "a.png", "mime": "image/png", "data_base64": "A" * 9_000_000}
                    ],
                }
            )
            reply = drain_until(app, "reply")
            assert reply["error"]["code"] == "too_large"


async def test_one_maximal_forward_at_a_time(tmp_path: Path) -> None:
    """GW-1: two protocol-legal maximal sends at once are more memory than the container has."""
    from rc_gateway.budget import MAX_INFLIGHT_BYTES
    from rc_gateway.frames import MAX_ATTACHMENT_BYTES

    rig = await hub_rig(tmp_path)
    await rig.hub._store_session(rig.device_id, session_summary(SESSION_ID, rig.device_id))
    payload = "A" * MAX_ATTACHMENT_BYTES
    frame = {
        "type": "session.send",
        "session_id": SESSION_ID,
        "text": "here you go",
        "attachments": [
            {"name": f"photo-{index}.jpg", "mime": "image/jpeg", "data_base64": payload}
            for index in range(8)
        ],
    }
    assert 8 * MAX_ATTACHMENT_BYTES > MAX_INFLIGHT_BYTES // 2

    # The device's queue is never drained here, which is what a device on a slow link looks like.
    await rig.hub.handle_app_frame(rig.app, {**frame, "id": "first"})
    await rig.hub.handle_app_frame(rig.app, {**frame, "id": "second"})
    replies = [item for item in frames_of(rig.app) if item.get("type") == "reply"]
    assert len(replies) == 1
    assert replies[0]["id"] == "second"
    assert replies[0]["error"]["code"] == "too_large"
    assert rig.hub._budget.held == 8 * MAX_ATTACHMENT_BYTES

    # Draining the device gives the budget back, and the next large send goes through.
    rig.device.drain()
    assert rig.hub._budget.held == 0
    await rig.hub.handle_app_frame(rig.app, {**frame, "id": "third"})
    assert [item.get("type") for item in frames_of(rig.app)] == []
    await rig.hub.stop()


async def test_a_queued_frame_is_measured_without_a_second_copy(tmp_path: Path) -> None:
    """GW-1: the accounting used to encode the payload twice, once only to read its length."""
    rig = await hub_rig(tmp_path)
    await rig.app.send({"type": "ping", "text": "héllo"})
    item = rig.app.queue.get_nowait()
    assert item.size == sys.getsizeof(item.raw)
    await rig.hub.stop()


async def test_an_account_holds_only_so_many_app_sockets(tmp_path: Path) -> None:
    """GW-7: nothing capped them, and every broadcast is linear in how many there are."""
    from rc_gateway.frames import CLOSE_TOO_MANY_APPS
    from rc_gateway.hub import MAX_APPS_PER_USER

    rig = await hub_rig(tmp_path)
    first = rig.app
    for _ in range(MAX_APPS_PER_USER - 1):
        await rig.hub.attach_app(fake_app())
    assert len(rig.hub._apps) == MAX_APPS_PER_USER

    await rig.hub.attach_app(fake_app())
    assert len(rig.hub._apps) == MAX_APPS_PER_USER
    assert first.id not in rig.hub._apps
    assert first.close_code == CLOSE_TOO_MANY_APPS

    # Another account's sockets are counted separately.
    await rig.hub.attach_app(fake_app("mallory"))
    assert len(rig.hub._apps) == MAX_APPS_PER_USER + 1
    await rig.hub.stop()


def test_an_announced_agent_list_is_bounded_on_the_socket_too(
    client: TestClient, auth: dict[str, str]
) -> None:
    """GW-10: `POST /api/devices/enroll` truncates to 16 and `hello` did not."""
    from rc_gateway.hub import MAX_AGENT_ENTRY_BYTES, MAX_ANNOUNCED_AGENTS

    enrolled = enroll_device(client, auth)
    announced = [{"agent": f"agent-{index}", "available": True} for index in range(40)]
    announced.append({"agent": "huge", "available": True, "path": "x" * MAX_AGENT_ENTRY_BYTES})
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(agents=announced))
        device.receive_json()
        listed = client.get("/api/devices", headers=auth).json()["devices"]
    assert len(listed[0]["agents"]) == MAX_ANNOUNCED_AGENTS
    assert all(item["agent"].startswith("agent-") for item in listed[0]["agents"])


def test_a_binary_frame_does_not_drop_the_socket(client: TestClient, auth: dict[str, str]) -> None:
    """GW-12: `receive_text` raised `KeyError` on one, costing the peer its link silently."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello())
        device.receive_json()
        device.send_bytes(b"\x00\x01\x02")
        summary = session_summary(SESSION_ID, enrolled["device_id"])
        device.send_json({"type": "session.updated", "session": summary})
        with client.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
    assert [item["session_id"] for item in hello["sessions"]] == [SESSION_ID]


def test_a_run_of_binary_frames_closes_with_a_protocol_error(
    client: TestClient, auth: dict[str, str]
) -> None:
    """GW-12: and a peer that will not stop is told why, instead of being abandoned."""
    from rc_gateway.frames import CLOSE_PROTOCOL_ERROR
    from rc_gateway.ws.text import MAX_CONSECUTIVE_BINARY

    with (
        pytest.raises(WebSocketDisconnect) as caught,
        client.websocket_connect("/ws/app", headers=auth) as app,
    ):
        drain_until(app, "hello")
        for _ in range(MAX_CONSECUTIVE_BINARY + 1):
            app.send_bytes(b"\x00")
        app.receive_json()
    assert caught.value.code == CLOSE_PROTOCOL_ERROR
