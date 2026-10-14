"""Subscription cursors, the replay buffer, resync and event fan-out."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from rc_gateway.replay import ReplayBuffer

from .conftest import device_hello, drain_until, enroll_device, session_summary

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _event(seq: int, text: str = "ok") -> dict[str, Any]:
    return {
        "seq": seq,
        "ts": 1710000000000 + seq,
        "kind": "assistant_text",
        "block_id": f"b{seq}",
        "text": text,
        "done": True,
    }


def _send_event(device: Any, seq: int, text: str = "ok") -> None:
    device.send_json(
        {"type": "session.event", "session_id": SESSION_ID, "event": _event(seq, text)}
    )


def _connect_device(client: TestClient, enrolled: dict[str, Any]) -> Any:
    return client.websocket_connect(
        "/ws/device", headers={"Authorization": f"Bearer {enrolled['device_token']}"}
    )


def test_replay_buffer_bounds_and_cursors() -> None:
    buffer = ReplayBuffer(max_events=3, max_bytes=10_000)
    for seq in range(1, 6):
        assert buffer.append(_event(seq), 100)
    assert len(buffer) == 3
    assert buffer.first_seq == 3
    assert buffer.last_seq == 5

    events, resync = buffer.replay_from(4)
    assert [item["seq"] for item in events] == [5]
    assert resync is False

    events, resync = buffer.replay_from(1)
    assert resync is True and events == []

    assert buffer.replay_from(None) == ([], False)
    assert buffer.replay_from(5) == ([], False)


def test_replay_buffer_rejects_late_and_duplicate_events() -> None:
    buffer = ReplayBuffer()
    assert buffer.append(_event(5), 50) is True
    assert buffer.append(_event(5), 50) is False
    assert buffer.append(_event(4), 50) is False
    assert buffer.append(_event(6), 50) is True


def test_replay_buffer_honours_the_byte_bound() -> None:
    buffer = ReplayBuffer(max_events=1000, max_bytes=250)
    for seq in range(1, 6):
        buffer.append(_event(seq), 100)
    assert buffer.byte_size <= 250
    assert len(buffer) == 2


def test_subscribe_returns_the_session_and_streams_events(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with _connect_device(client, enrolled) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            reply = drain_until(app, "reply")
            assert reply["ok"] is True
            assert reply["result"]["session"]["session_id"] == SESSION_ID
            assert reply["result"]["events"] == []
            assert reply["result"]["resync"] is False

            _send_event(device, 1, "first")
            streamed = drain_until(app, "session.event")
            assert streamed["session_id"] == SESSION_ID
            assert streamed["event"]["seq"] == 1
            assert streamed["event"]["text"] == "first"


def test_events_do_not_reach_unsubscribed_apps(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with _connect_device(client, enrolled) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            _send_event(device, 1)
            # Nothing is subscribed, so the only frame that can follow is the session summary
            # update the device sends next.
            device.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_ID, enrolled["device_id"], state="running"),
                }
            )
            assert drain_until(app, "session.updated")["session"]["state"] == "running"


def test_unsubscribe_stops_the_stream(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with _connect_device(client, enrolled) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            app.send_json({"type": "session.unsubscribe", "session_id": SESSION_ID})
            _send_event(device, 1)
            device.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_ID, enrolled["device_id"], state="error"),
                }
            )
            assert drain_until(app, "session.updated")["session"]["state"] == "error"


def test_resubscribing_with_a_cursor_replays_the_tail(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with _connect_device(client, enrolled) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as first:
            drain_until(first, "hello")
            first.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(first, "reply")
            for seq in range(1, 4):
                _send_event(device, seq)
                drain_until(first, "session.event")

        with client.websocket_connect("/ws/app", headers=auth) as second:
            drain_until(second, "hello")
            second.send_json(
                {
                    "type": "session.subscribe",
                    "id": "s2",
                    "session_id": SESSION_ID,
                    "since_seq": 1,
                }
            )
            reply = drain_until(second, "reply")
            assert [item["seq"] for item in reply["result"]["events"]] == [2, 3]
            assert reply["result"]["resync"] is False
            assert reply["result"]["session"]["last_seq"] == 3


def test_a_cursor_the_buffer_cannot_cover_asks_for_a_resync(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    with _connect_device(client, enrolled) as device:
        device.send_json(
            device_hello(
                sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=500)]
            )
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(
                {
                    "type": "session.subscribe",
                    "id": "s3",
                    "session_id": SESSION_ID,
                    "since_seq": 10,
                }
            )
            reply = drain_until(app, "reply")
            assert reply["result"]["resync"] is True
            assert reply["result"]["events"] == []


def test_subscribing_to_an_unknown_session_is_not_found(
    client: TestClient, auth: dict[str, str]
) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        app.send_json({"type": "session.subscribe", "id": "s4", "session_id": "missing"})
        reply = drain_until(app, "reply")
        assert reply["error"]["code"] == "not_found"


def test_oversized_events_are_dropped(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with _connect_device(client, enrolled) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s5", "session_id": SESSION_ID})
            drain_until(app, "reply")
            _send_event(device, 1, "x" * 70_000)
            _send_event(device, 2, "small")
            streamed = drain_until(app, "session.event")
            assert streamed["event"]["seq"] == 2
