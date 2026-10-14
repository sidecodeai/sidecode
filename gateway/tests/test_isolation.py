"""One device must not reach another device's sessions, and one account not another's.

Every enrolled machine is a separate trust domain: a compromised or buggy client must not be able
to hijack, forge, silence or delete the sessions of another. Accounts (A24) draw the second
boundary, above it: a signed-in socket sees the devices its own account enrolled, the sessions on
them and nothing else, and a request naming anything outside that is answered as if it did not
exist.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from rc_gateway.state import GatewayState

from .conftest import add_member, device_hello, drain_until, enroll_device, session_summary

SESSION_A = "aaaa1111-2222-4333-8444-555555555555"
SESSION_B = "bbbb1111-2222-4333-8444-555555555555"


def _headers(enrolled: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {enrolled['device_token']}"}


def test_a_device_cannot_take_over_another_devices_session(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    victim = enroll_device(client, auth, name="victim")
    attacker = enroll_device(client, auth, name="attacker")
    with client.websocket_connect("/ws/device", headers=_headers(victim)) as owner:
        owner.send_json(device_hello(sessions=[session_summary(SESSION_B, victim["device_id"])]))
        owner.receive_json()
        with (
            client.websocket_connect("/ws/app", headers=auth) as app,
            client.websocket_connect("/ws/device", headers=_headers(attacker)) as intruder,
        ):
            drain_until(app, "hello")
            intruder.send_json(device_hello())
            intruder.receive_json()
            intruder.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_B, attacker["device_id"], title="stolen"),
                }
            )
            # A frame the gateway ignores produces no observable effect, so settle on a frame it
            # does act on before asserting: leaving the socket cancels whatever it has not read.
            intruder.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_A, attacker["device_id"]),
                }
            )
            assert drain_until(app, "session.updated")["session"]["session_id"] == SESSION_A
    listed = client.get("/api/sessions", headers=auth).json()["sessions"]
    owners = {item["session_id"]: item["device_id"] for item in listed}
    assert owners[SESSION_B] == victim["device_id"]
    assert owners[SESSION_A] == attacker["device_id"]
    assert next(item for item in listed if item["session_id"] == SESSION_B)["title"] != "stolen"


def test_a_device_cannot_inject_events_into_another_devices_session(
    client: TestClient, auth: dict[str, str]
) -> None:
    victim = enroll_device(client, auth, name="victim")
    attacker = enroll_device(client, auth, name="attacker")
    with client.websocket_connect("/ws/device", headers=_headers(victim)) as owner:
        owner.send_json(device_hello(sessions=[session_summary(SESSION_B, victim["device_id"])]))
        owner.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_B})
            drain_until(app, "reply")
            with client.websocket_connect("/ws/device", headers=_headers(attacker)) as intruder:
                intruder.send_json(device_hello())
                intruder.receive_json()
                intruder.send_json(
                    {
                        "type": "session.event",
                        "session_id": SESSION_B,
                        "event": {
                            "seq": 9_000_000,
                            "ts": 1,
                            "kind": "assistant_text",
                            "block_id": "forged",
                            "text": "INJECTED",
                            "done": True,
                        },
                    }
                )
            # The genuine event still arrives, and its low seq was never poisoned by the forgery.
            owner.send_json(
                {
                    "type": "session.event",
                    "session_id": SESSION_B,
                    "event": {
                        "seq": 5,
                        "ts": 2,
                        "kind": "assistant_text",
                        "block_id": "real",
                        "text": "genuine",
                        "done": True,
                    },
                }
            )
            streamed = drain_until(app, "session.event")
    assert streamed["event"]["text"] == "genuine"
    assert streamed["event"]["seq"] == 5
    assert streamed["device_id"] == victim["device_id"]


def test_a_device_cannot_delete_another_devices_session(
    client: TestClient, auth: dict[str, str]
) -> None:
    victim = enroll_device(client, auth, name="victim")
    attacker = enroll_device(client, auth, name="attacker")
    with client.websocket_connect("/ws/device", headers=_headers(victim)) as owner:
        owner.send_json(device_hello(sessions=[session_summary(SESSION_B, victim["device_id"])]))
        owner.receive_json()
        with client.websocket_connect("/ws/device", headers=_headers(attacker)) as intruder:
            intruder.send_json(device_hello())
            intruder.receive_json()
            intruder.send_json({"type": "session.removed", "session_id": SESSION_B})
            intruder.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_A, attacker["device_id"]),
                }
            )
            with client.websocket_connect("/ws/app", headers=auth) as app:
                drain_until(app, "hello")
    listed = {
        item["session_id"] for item in client.get("/api/sessions", headers=auth).json()["sessions"]
    }
    assert SESSION_B in listed


def test_a_device_cannot_answer_a_request_pending_on_another(
    client: TestClient, auth: dict[str, str]
) -> None:
    victim = enroll_device(client, auth, name="victim")
    attacker = enroll_device(client, auth, name="attacker")
    with client.websocket_connect("/ws/device", headers=_headers(victim)) as owner:
        owner.send_json(device_hello(sessions=[session_summary(SESSION_B, victim["device_id"])]))
        owner.receive_json()
        with client.websocket_connect("/ws/device", headers=_headers(attacker)) as intruder:
            intruder.send_json(device_hello())
            intruder.receive_json()
            with client.websocket_connect("/ws/app", headers=auth) as app:
                drain_until(app, "hello")
                app.send_json(
                    {"type": "session.send", "id": "r1", "session_id": SESSION_B, "text": "hi"}
                )
                forwarded = drain_until(owner, "session.send")
                intruder.send_json(
                    {
                        "type": "reply",
                        "id": "r1",
                        "from": forwarded["from"],
                        "ok": True,
                        "result": {"accepted": "sent", "forged": True},
                    }
                )
                owner.send_json(
                    {
                        "type": "reply",
                        "id": "r1",
                        "from": forwarded["from"],
                        "ok": True,
                        "result": {"accepted": "queued"},
                    }
                )
                reply = drain_until(app, "reply")
    assert reply["result"] == {"accepted": "queued"}


def test_ownership_survives_a_reconnect(client: TestClient, auth: dict[str, str]) -> None:
    """The binding is set at first sight and is not re-derived from whoever speaks next."""
    victim = enroll_device(client, auth, name="victim")
    attacker = enroll_device(client, auth, name="attacker")
    with client.websocket_connect("/ws/device", headers=_headers(victim)) as owner:
        owner.send_json(device_hello(sessions=[session_summary(SESSION_B, victim["device_id"])]))
        owner.receive_json()
    # The owner is offline; the attacker announces the same session in its own hello.
    with client.websocket_connect("/ws/device", headers=_headers(attacker)) as intruder:
        intruder.send_json(
            device_hello(sessions=[session_summary(SESSION_B, attacker["device_id"])])
        )
        intruder.receive_json()
    listed = client.get("/api/sessions", headers=auth).json()["sessions"]
    assert [item["device_id"] for item in listed] == [victim["device_id"]]


def test_one_account_never_sees_another_accounts_device_or_sessions(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A24: every frame after `hello` concerns the account that owns the device."""
    member = add_member(client, auth, "alice")
    mine = enroll_device(client, auth, name="admin-mac")
    theirs = enroll_device(client, member, name="alice-mac")
    with client.websocket_connect("/ws/app", headers=member) as watcher:
        drain_until(watcher, "hello")
        with client.websocket_connect("/ws/device", headers=_headers(mine)) as device:
            device.send_json(device_hello(sessions=[session_summary(SESSION_A, mine["device_id"])]))
            device.receive_json()
            device.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_A, mine["device_id"], state="running"),
                }
            )
            device.send_json({"type": "session.removed", "session_id": SESSION_A})
        # The member's own device speaking is the settle point: everything the other device
        # produced was published before it, so a quiet socket here means nothing leaked.
        with client.websocket_connect("/ws/device", headers=_headers(theirs)) as own:
            own.send_json(device_hello(sessions=[session_summary(SESSION_B, theirs["device_id"])]))
            own.receive_json()
            first = drain_until(watcher, "device.updated")
        assert first["device"]["device_id"] == theirs["device_id"]


def test_a_member_cannot_subscribe_to_or_address_another_accounts_session(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "alice")
    mine = enroll_device(client, auth, name="admin-mac")
    with client.websocket_connect("/ws/device", headers=_headers(mine)) as device:
        device.send_json(device_hello(sessions=[session_summary(SESSION_A, mine["device_id"])]))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=member) as intruder:
            drain_until(intruder, "hello")
            intruder.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_A})
            subscribed = drain_until(intruder, "reply")
            intruder.send_json(
                {"type": "session.send", "id": "r1", "session_id": SESSION_A, "text": "hi"}
            )
            sent = drain_until(intruder, "reply")
            intruder.send_json(
                {
                    "type": "session.create",
                    "id": "c1",
                    "device_id": mine["device_id"],
                    "agent": "claude",
                    "cwd": "/tmp",
                }
            )
            created = drain_until(intruder, "reply")
    # Indistinguishable from a session that does not exist: no probing which ids are real.
    assert subscribed["error"]["code"] == "not_found"
    assert sent["error"]["code"] == "not_found"
    assert created["error"]["code"] == "not_found"


def test_a_member_lists_only_its_own_devices_and_sessions(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "alice")
    mine = enroll_device(client, auth, name="admin-mac")
    theirs = enroll_device(client, member, name="alice-mac")
    for enrolled, session_id in ((mine, SESSION_A), (theirs, SESSION_B)):
        with client.websocket_connect("/ws/device", headers=_headers(enrolled)) as device:
            device.send_json(
                device_hello(sessions=[session_summary(session_id, enrolled["device_id"])])
            )
            device.receive_json()

    listed = client.get("/api/sessions", headers=member).json()["sessions"]
    assert [item["session_id"] for item in listed] == [SESSION_B]
    devices = client.get("/api/devices", headers=member).json()["devices"]
    assert [item["device_id"] for item in devices] == [theirs["device_id"]]
    # Naming the other account's device narrows the member's own set to nothing.
    filtered = client.get(
        "/api/sessions", params={"device_id": mine["device_id"]}, headers=member
    ).json()["sessions"]
    assert filtered == []

    with client.websocket_connect("/ws/app", headers=member) as app:
        hello = drain_until(app, "hello")
    assert [item["device_id"] for item in hello["devices"]] == [theirs["device_id"]]
    assert [item["session_id"] for item in hello["sessions"]] == [SESSION_B]


def test_pairing_progress_reaches_only_the_account_that_minted_the_code(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "alice")
    with client.websocket_connect("/ws/app", headers=member) as watcher:
        drain_until(watcher, "hello")
        enroll_device(client, auth, name="admin-mac")
        member_code = client.post("/api/devices/pairing", headers=member).json()["code"]
        progress = drain_until(watcher, "pairing.progress")
    assert progress["code"] == member_code
    assert progress["step"] == "waiting"
