"""Amendments A4 (close codes), A5 (`device_id` on forwarded frames) and A6 (queue snapshot)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rc_gateway.connections import DeviceConnection
from rc_gateway.frames import (
    CLOSE_DEVICE_REPLACED,
    CLOSE_PROTOCOL_ERROR,
    CLOSE_UNAUTHORIZED,
)
from rc_gateway.state import GatewayState

from .conftest import (
    ORIGIN,
    HubRig,
    close_code_for,
    collect_until,
    device_hello,
    drain_until,
    enroll_device,
    fake_device,
    frames_of,
    hub_rig,
    session_summary,
)

SESSION_ID = "cccc1111-2222-4333-8444-555555555555"
#: How long a test that answers the gateway's request waits for it to be asked.
PATIENT = 5.0


def _headers(enrolled: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {enrolled['device_token']}"}


def _close_code(excinfo: pytest.ExceptionInfo[WebSocketDisconnect]) -> int:
    return int(excinfo.value.code)


def test_app_socket_without_a_credential_closes_4401(client: TestClient) -> None:
    assert close_code_for(client, "/ws/app") == CLOSE_UNAUTHORIZED


def test_app_socket_with_an_expired_token_closes_4401(client: TestClient, state: Any) -> None:
    from rc_gateway.auth import make_session_token

    token, _ = make_session_token(state.config.secret, -10)
    code = close_code_for(client, "/ws/app", headers={"Authorization": f"Bearer {token}"})
    assert code == CLOSE_UNAUTHORIZED


def test_a_cookie_socket_from_a_foreign_origin_closes_4403(client: TestClient, token: str) -> None:
    from rc_gateway.auth import SESSION_COOKIE_NAME
    from rc_gateway.frames import CLOSE_FORBIDDEN

    client.cookies.set(SESSION_COOKIE_NAME, token)
    code = close_code_for(client, "/ws/app", headers={"Origin": "https://evil.example"})
    assert code == CLOSE_FORBIDDEN
    with client.websocket_connect("/ws/app", headers={"Origin": ORIGIN}) as app:
        assert drain_until(app, "hello")["protocol"] == 1


def test_device_socket_with_a_bad_token_closes_4401(client: TestClient) -> None:
    code = close_code_for(client, "/ws/device", headers={"Authorization": "Bearer " + "z" * 64})
    assert code == CLOSE_UNAUTHORIZED


def test_a_protocol_violation_closes_1008(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json({"type": "session.updated", "session": {}})
        with pytest.raises(WebSocketDisconnect) as caught:
            device.receive_json()
    assert _close_code(caught) == CLOSE_PROTOCOL_ERROR

    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(device_hello(protocol=99))
        with pytest.raises(WebSocketDisconnect) as caught:
            device.receive_json()
    assert _close_code(caught) == CLOSE_PROTOCOL_ERROR


def test_device_replacement_closes_4001(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as first:
        first.send_json(device_hello())
        first.receive_json()
        with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as second:
            second.send_json(device_hello())
            second.receive_json()
            with pytest.raises(WebSocketDisconnect) as caught:
                for _ in range(5):
                    first.receive_json()
    assert _close_code(caught) == CLOSE_DEVICE_REPLACED


def test_revoking_a_device_closes_4401(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(device_hello())
        device.receive_json()
        client.delete(f"/api/devices/{enrolled['device_id']}", headers=auth)
        with pytest.raises(WebSocketDisconnect) as caught:
            for _ in range(5):
                device.receive_json()
    assert _close_code(caught) == CLOSE_UNAUTHORIZED


def test_logout_closes_the_app_socket_with_4401(client: TestClient, auth: dict[str, str]) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        assert client.post("/api/logout", headers=auth).status_code == 200
        with pytest.raises(WebSocketDisconnect) as caught:
            for _ in range(5):
                app.receive_json()
    assert _close_code(caught) == CLOSE_UNAUTHORIZED


def test_forwarded_session_frames_carry_device_id(client: TestClient, auth: dict[str, str]) -> None:
    """A5: apps learn the owning device from the frame, not from a separate lookup."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            device.send_json(
                {
                    "type": "session.event",
                    "session_id": SESSION_ID,
                    "event": {"seq": 1, "ts": 1, "kind": "notice", "level": "info", "text": "x"},
                }
            )
            event = drain_until(app, "session.event")
            assert event["device_id"] == enrolled["device_id"]

            device.send_json({"type": "session.removed", "session_id": SESSION_ID})
            removed = drain_until(app, "session.removed")
            assert removed["device_id"] == enrolled["device_id"]


def test_the_device_id_on_events_comes_from_the_token(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A device claiming a different `device_id` in the frame does not get to keep it."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            device.send_json(
                {
                    "type": "session.event",
                    "session_id": SESSION_ID,
                    "device_id": "someone-else",
                    "event": {"seq": 1, "ts": 1, "kind": "notice", "level": "info", "text": "x"},
                }
            )
            event = drain_until(app, "session.event")
    assert event["device_id"] == enrolled["device_id"]


def test_subscribe_returns_the_latest_queue_snapshot(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A6: a queue snapshot describes current state, so a late subscriber still sees it."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as first:
            drain_until(first, "hello")
            first.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            reply = drain_until(first, "reply")
            assert "queue" not in reply["result"]

            for seq, pending in ((1, [{"id": "q1", "text": "one", "ts": 1}]), (2, [])):
                device.send_json(
                    {
                        "type": "session.event",
                        "session_id": SESSION_ID,
                        "event": {"seq": seq, "ts": seq, "kind": "queue", "pending": pending},
                    }
                )
                drain_until(first, "session.event")
            device.send_json(
                {
                    "type": "session.event",
                    "session_id": SESSION_ID,
                    "event": {
                        "seq": 3,
                        "ts": 3,
                        "kind": "queue",
                        "pending": [{"id": "q2", "text": "two", "ts": 3}],
                    },
                }
            )
            drain_until(first, "session.event")

        with client.websocket_connect("/ws/app", headers=auth) as second:
            drain_until(second, "hello")
            second.send_json({"type": "session.subscribe", "id": "s2", "session_id": SESSION_ID})
            reply = drain_until(second, "reply")
    assert reply["result"]["queue"] == {"pending": [{"id": "q2", "text": "two", "ts": 3}]}


async def _held_queue(rig: HubRig, identifier: str) -> Any:
    """The queue snapshot a subscriber with no cursor is handed, or ``None`` when there is none."""
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.subscribe", "id": identifier, "session_id": SESSION_ID}
    )
    (reply,) = [item for item in frames_of(rig.app) if item.get("id") == identifier]
    return reply["result"].get("queue")


async def _summarise(rig: HubRig, connection: DeviceConnection, **fields: Any) -> None:
    summary = session_summary(SESSION_ID, connection.device_id, **fields)
    await rig.hub.handle_device_frame(connection, {"type": "session.updated", "session": summary})


async def _queue_up(rig: HubRig, seq: int, *texts: str) -> None:
    pending = [{"id": f"q{seq}-{n}", "text": text, "ts": seq} for n, text in enumerate(texts)]
    await rig.hub.handle_device_frame(
        rig.device,
        {
            "type": "session.event",
            "session_id": SESSION_ID,
            "event": {"seq": seq, "ts": seq, "kind": "queue", "pending": pending},
        },
    )


@pytest.mark.asyncio
async def test_a_summary_that_says_nothing_is_queued_drops_the_queue_snapshot(
    tmp_path: Path,
) -> None:
    """A56: a summary says what a lost queue frame could not, so the stale snapshot goes."""
    rig = await hub_rig(tmp_path)
    await _summarise(rig, rig.device, queued=0)
    await _queue_up(rig, 1, "one", "two")
    held = await _held_queue(rig, "s1")
    assert held is not None and len(held["pending"]) == 2

    # A summary that still counts queued messages leaves what the gateway holds as it is.
    await _summarise(rig, rig.device, queued=2)
    assert await _held_queue(rig, "s2") == held
    # So does one from a device that does not say.
    silent = session_summary(SESSION_ID, rig.device_id)
    del silent["queued"]
    await rig.hub.handle_device_frame(rig.device, {"type": "session.updated", "session": silent})
    assert await _held_queue(rig, "s3") == held

    await _summarise(rig, rig.device, queued=0)
    assert await _held_queue(rig, "s4") is None
    # Nothing is made up for a summary that counts queued messages and has no snapshot to keep.
    await _summarise(rig, rig.device, queued=1)
    assert await _held_queue(rig, "s5") is None
    # And a queue event sets the snapshot as it always did.
    await _queue_up(rig, 2, "three")
    again = await _held_queue(rig, "s6")
    assert again is not None and [item["text"] for item in again["pending"]] == ["three"]

    # A summary in a device's hello says the same.
    hello = device_hello(sessions=[session_summary(SESSION_ID, rig.device_id, queued=0)])
    await rig.hub.handle_device_hello(rig.device, hello)
    assert await _held_queue(rig, "s7") is None
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_summary_from_a_device_that_does_not_own_the_session_keeps_its_queue_snapshot(
    tmp_path: Path,
) -> None:
    rig = await hub_rig(tmp_path)
    await _summarise(rig, rig.device, queued=1)
    await _queue_up(rig, 1, "one")
    held = await _held_queue(rig, "s1")
    assert held is not None

    stranger = fake_device("a-device-that-does-not-own-the-session")
    await _summarise(rig, stranger, queued=0)

    assert await _held_queue(rig, "s2") == held
    await rig.hub.stop()


def test_a_reconnecting_device_backfills_what_the_buffer_missed(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    """A9: the gateway asks for the tail it missed and replays it to current subscribers."""
    state.hub.fetch_timeout = PATIENT
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            for seq in (1, 2):
                device.send_json(
                    {
                        "type": "session.event",
                        "session_id": SESSION_ID,
                        "event": {
                            "seq": seq,
                            "ts": seq,
                            "kind": "notice",
                            "level": "info",
                            "text": f"live-{seq}",
                        },
                    }
                )
                drain_until(app, "session.event")

            # The link drops and the device comes back having advanced to seq 5.
            with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as revived:
                revived.send_json(
                    device_hello(
                        sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=5)]
                    )
                )
                drain_until(revived, "hello_ack")

                request = drain_until(revived, "session.history")
                assert request["after_seq"] == 2
                assert request["limit"] == 500
                assert request["from"] == "gateway"
                assert request["device_id"] == enrolled["device_id"]
                assert request["session_id"] == SESSION_ID

                revived.send_json(
                    {
                        "type": "reply",
                        "id": request["id"],
                        "from": "gateway",
                        "ok": True,
                        "result": {
                            "events": [
                                {
                                    "seq": seq,
                                    "ts": seq,
                                    "kind": "notice",
                                    "level": "info",
                                    "text": f"missed-{seq}",
                                }
                                # seq 2 is already buffered and must be skipped.
                                for seq in (2, 3, 4, 5)
                            ],
                            "has_more": False,
                        },
                    }
                )
                replayed = [drain_until(app, "session.event") for _ in range(3)]

    assert [item["event"]["seq"] for item in replayed] == [3, 4, 5]
    assert [item["event"]["text"] for item in replayed] == ["missed-3", "missed-4", "missed-5"]
    assert {item["device_id"] for item in replayed} == {enrolled["device_id"]}


def test_a_backfill_that_does_not_fit_is_not_delivered_in_part(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    """A56: a gap past what one fetch may take is left to `resync`, never half filled.

    A hole the gateway cannot close is worse than a resync, so a reply that says there is more
    delivers nothing, and an app that notices the gap and subscribes again is told to resynchronise.
    """
    state.hub.fetch_timeout = PATIENT
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            device.send_json(
                {
                    "type": "session.event",
                    "session_id": SESSION_ID,
                    "event": {"seq": 1, "ts": 1, "kind": "notice", "level": "info", "text": "a"},
                }
            )
            drain_until(app, "session.event")

            with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as revived:
                revived.send_json(
                    device_hello(
                        sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=4)]
                    )
                )
                drain_until(revived, "hello_ack")
                request = drain_until(revived, "session.history")
                assert request["after_seq"] == 1
                revived.send_json(
                    {
                        "type": "reply",
                        "id": request["id"],
                        "from": "gateway",
                        "ok": True,
                        "result": {
                            "events": [
                                {
                                    "seq": seq,
                                    "ts": seq,
                                    "kind": "notice",
                                    "level": "info",
                                    "text": f"p{seq}",
                                }
                                for seq in (2, 3)
                            ],
                            "has_more": True,
                        },
                    }
                )
                # The next live event is the app's first since: nothing of 2 and 3 came before it.
                revived.send_json(
                    {
                        "type": "session.event",
                        "session_id": SESSION_ID,
                        "event": {
                            "seq": 5,
                            "ts": 5,
                            "kind": "notice",
                            "level": "info",
                            "text": "live",
                        },
                    }
                )
                assert drain_until(app, "session.event")["event"]["seq"] == 5
                app.send_json(
                    {
                        "type": "session.subscribe",
                        "id": "repair",
                        "session_id": SESSION_ID,
                        "since_seq": 5,
                    }
                )
                assert drain_until(app, "reply")["result"]["resync"] is True


def test_an_app_subscribed_before_its_device_returns_is_given_what_it_missed(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    """A56: after a restart the buffer is empty, and an app that subscribed first still catches up.

    The gateway knew the session at seq 50 and the app subscribed from there, so the subscribe was
    answered without a resync. The device had gone on to 60 while the gateway was down, and when it
    says hello the gateway asks it for those events, so the next live event leaves no gap.
    """
    state.hub.fetch_timeout = PATIENT
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=50)])
        )
        drain_until(device, "hello_ack")
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        app.send_json(
            {"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID, "since_seq": 50}
        )
        assert drain_until(app, "reply")["result"]["resync"] is False

        with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as revived:
            revived.send_json(
                device_hello(
                    sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=60)]
                )
            )
            drain_until(revived, "hello_ack")
            # A forwarded request is the marker: the gateway's request, if there is one, is queued
            # ahead of it, and without one the test fails here instead of waiting for a frame.
            time.sleep(0.2)
            app.send_json({"type": "session.stop", "id": "marker", "session_id": SESSION_ID})
            request = next(
                frame
                for frame in collect_until(revived, "session.stop")
                if frame["type"] == "session.history"
            )
            assert request["after_seq"] == 50
            revived.send_json(
                {
                    "type": "reply",
                    "id": request["id"],
                    "from": "gateway",
                    "ok": True,
                    "result": {
                        "events": [
                            {
                                "seq": seq,
                                "ts": seq,
                                "kind": "notice",
                                "level": "info",
                                "text": f"outage-{seq}",
                            }
                            for seq in range(51, 61)
                        ],
                        "has_more": False,
                    },
                }
            )
            filled = [drain_until(app, "session.event") for _ in range(10)]
    assert [item["event"]["seq"] for item in filled] == list(range(51, 61))


def test_no_backfill_when_nobody_is_behind_and_the_buffer_is_current(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Nothing is fetched for a session no app waits on, nor for a buffer that is up to date.

    An empty buffer is not a reason to skip a session (A56): an app that waits behind the device
    is. Nobody is subscribed in this test.
    """
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        # Nothing has ever been buffered for this session, however far ahead the device is.
        device.send_json(
            device_hello(
                sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=900)]
            )
        )
        drain_until(device, "hello_ack")
        device.send_json(
            {
                "type": "session.event",
                "session_id": SESSION_ID,
                "event": {"seq": 901, "ts": 1, "kind": "notice", "level": "info", "text": "x"},
            }
        )
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            # A reconnect whose last_seq matches the buffer tail must ask for nothing. A forwarded
            # request acts as the marker: the backfill would be dispatched during hello handling,
            # so it would appear before this.
            with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as revived:
                revived.send_json(
                    device_hello(
                        sessions=[session_summary(SESSION_ID, enrolled["device_id"], last_seq=901)]
                    )
                )
                drain_until(revived, "hello_ack")
                app.send_json({"type": "session.stop", "id": "marker", "session_id": SESSION_ID})
                seen = collect_until(revived, "session.stop")
    # A35 sends the account's switches after the ack, on every connect, so that one frame may
    # land here too; what must not appear is a `session.history` request.
    assert [frame["type"] for frame in seen if frame["type"] != "preferences"] == ["session.stop"]


def test_a_backfill_is_requested_only_for_sessions_that_fell_behind(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Two sessions, one current and one behind: only the stale one is fetched."""
    other = "eeee1111-2222-4333-8444-555555555555"
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
        device.send_json(
            device_hello(
                sessions=[
                    session_summary(SESSION_ID, enrolled["device_id"]),
                    session_summary(other, enrolled["device_id"]),
                ]
            )
        )
        drain_until(device, "hello_ack")
        for session_id in (SESSION_ID, other):
            device.send_json(
                {
                    "type": "session.event",
                    "session_id": session_id,
                    "event": {
                        "seq": 1,
                        "ts": 1,
                        "kind": "notice",
                        "level": "info",
                        "text": "seed",
                    },
                }
            )
        with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as revived:
            revived.send_json(
                device_hello(
                    sessions=[
                        session_summary(SESSION_ID, enrolled["device_id"], last_seq=1),
                        session_summary(other, enrolled["device_id"], last_seq=9),
                    ]
                )
            )
            drain_until(revived, "hello_ack")
            request = drain_until(revived, "session.history")
    assert request["session_id"] == other
    assert request["after_seq"] == 1
