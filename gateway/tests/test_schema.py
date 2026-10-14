"""Validate real gateway output against `protocol/schema/*.json`.

The other tests compare key sets, which cannot catch a value in the wrong shape. These validate
what the gateway actually emits against the normative schema every other component checks against,
so a `device_id` that is not a UUID, or a timestamp in seconds, fails here rather than in the iOS
verification run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.badge import BadgeChange
from rc_gateway.push import PushService
from rc_gateway.push_store import ApnsRegistration, PushStore
from rc_gateway.state import GatewayState

from .conftest import (
    FIXTURE_DIR,
    ApnsRecorder,
    FakePolisher,
    FakeWebPushSender,
    apns_provider,
    device_hello,
    drain_until,
    enroll_device,
    make_config,
    session_summary,
    write_wheel,
)

jsonschema = pytest.importorskip("jsonschema")
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "protocol" / "schema"
pytestmark = pytest.mark.skipif(not SCHEMA_DIR.is_dir(), reason="protocol/schema is not present")

SESSION_ID = "dddd1111-2222-4333-8444-555555555555"


def _registry() -> Any:
    from referencing import Registry, Resource

    registry = Registry()
    for path in SCHEMA_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(document["$id"], Resource.from_contents(document))
    return registry


def check(frame: Any, schema_file: str, definition: str) -> None:
    """Validate one frame against `<schema_file>#/$defs/<definition>`."""
    document = json.loads((SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        {"$ref": f"{document['$id']}#/$defs/{definition}"}, registry=_registry()
    )
    errors = sorted(validator.iter_errors(frame), key=lambda item: list(item.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


def test_enroll_response_matches_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    """Finding 7: `device_id` is typed as a UUID everywhere it appears."""
    code = client.post("/api/devices/pairing", headers=auth).json()["code"]
    response = client.post(
        "/api/devices/enroll",
        json={
            "code": code,
            "name": "mac-studio-office",
            "platform": "macos",
            "hostname": "mac-studio.local",
            "arch": "arm64",
            "client_version": "0.1.0",
        },
    )
    check(response.json(), "http.json", "EnrollResponse")


def test_health_and_pairing_responses_match_the_schema(
    client: TestClient, auth: dict[str, str]
) -> None:
    check(client.get("/api/health").json(), "http.json", "HealthResponse")
    check(client.post("/api/devices/pairing", headers=auth).json(), "http.json", "PairingResponse")


def test_device_list_matches_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect(
        "/ws/device", headers={"Authorization": f"Bearer {enrolled['device_token']}"}
    ) as device:
        device.send_json(device_hello(client_build="a" * 64))
        device.receive_json()
    check(client.get("/api/devices", headers=auth).json(), "http.json", "DeviceListResponse")


def test_config_and_pairing_request_bodies_match_the_schema(
    tmp_path: Path, state: GatewayState, client: TestClient, auth: dict[str, str]
) -> None:
    """A22 and A23: the served wheel, the claim token, the poll and the claim."""
    write_wheel(tmp_path)
    state.pairing_requests.poll_timeout = 0.05
    check(client.get("/api/config", headers=auth).json(), "http.json", "ConfigResponse")

    minted = client.post("/api/pairing/requests").json()
    check(minted, "http.json", "PairingRequestResponse")
    token = minted["token"]
    check(
        client.get(f"/api/pairing/requests/{token}").json(),
        "http.json",
        "PairingRequestStatusResponse",
    )
    claimed = client.post(f"/api/pairing/requests/{token}/claim", headers=auth).json()
    check(claimed, "http.json", "PairingClaimResponse")
    check(
        client.get(f"/api/pairing/requests/{token}").json(),
        "http.json",
        "PairingRequestStatusResponse",
    )


def test_polish_bodies_match_the_schema(
    client: TestClient, auth: dict[str, str], polisher: FakePolisher
) -> None:
    """A29: the model list, the body the apps send and the answer they get back."""
    check(
        client.get("/api/polish/models", headers=auth).json(), "http.json", "PolishModelsResponse"
    )
    request = json.loads((FIXTURE_DIR / "http" / "polish.request.json").read_text(encoding="utf-8"))
    check(request, "http.json", "PolishRequest")
    answered = client.post("/api/polish", json=request, headers=auth)
    assert answered.status_code == 200, answered.text
    check(answered.json(), "http.json", "PolishResponse")


def test_preferences_bodies_match_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    """A35 and A41: the object with nothing set, the body that sets every field, and the answer."""
    check(client.get("/api/preferences", headers=auth).json(), "http.json", "PreferencesResponse")
    request = json.loads(
        (FIXTURE_DIR / "http" / "preferences.response.json").read_text(encoding="utf-8")
    )["preferences"]
    check(request, "http.json", "PreferencesPatchRequest")
    answered = client.patch("/api/preferences", json=request, headers=auth)
    assert answered.status_code == 200, answered.text
    check(answered.json(), "http.json", "PreferencesResponse")


def test_hello_ack_and_app_hello_match_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        ack = device.receive_json()
        check(ack, "device_frames.json", "HelloAck")
        with client.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
    check(hello, "app_frames.json", "Hello")


def test_apps_minimums_match_the_schema_in_all_three_bodies(tmp_path: Path) -> None:
    """A31, A45, A46: every entry of `apps`, each with its `update_url`, in all three bodies.

    The schema requires only `ios`, so each body is also checked for the other apps' entries.
    """
    config = make_config(
        tmp_path,
        ios_minimum_version="2.3.4",
        ios_update_url="https://testflight.apple.com/join/EXAMPLE",
        macos_minimum_version="1.12.0",
        macos_update_url="https://example.com/remote-control-mac",
        android_minimum_version="1.13.0",
        android_update_url="https://example.com/remote-control-android",
        windows_minimum_version="1.14.0",
        windows_update_url="https://example.com/remote-control-windows",
    )
    write_wheel(tmp_path)
    with TestClient(create_app(build_state(config))) as configured:
        health = configured.get("/api/health").json()
        check(health, "http.json", "HealthResponse")
        token = configured.post(
            "/api/login",
            json={"username": "admin", "password": config.password},
            headers={"Origin": config.public_origin},
        ).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        served = configured.get("/api/config", headers=auth).json()
        check(served, "http.json", "ConfigResponse")
        with configured.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
        check(hello, "app_frames.json", "Hello")
    for body in (health, served, hello):
        assert set(body["apps"]) == {"ios", "macos", "android", "windows"}


def test_pushed_app_frames_match_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            check(drain_until(app, "device.updated"), "app_frames.json", "DeviceUpdated")
            device.send_json(
                {
                    "type": "session.updated",
                    "session": session_summary(SESSION_ID, enrolled["device_id"]),
                }
            )
            check(drain_until(app, "session.updated"), "app_frames.json", "SessionUpdated")
            app.send_json({"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            device.send_json(
                {
                    "type": "session.event",
                    "session_id": SESSION_ID,
                    "event": {"seq": 1, "ts": 1, "kind": "notice", "level": "info", "text": "x"},
                }
            )
            check(drain_until(app, "session.event"), "app_frames.json", "SessionEvent")
            device.send_json({"type": "session.removed", "session_id": SESSION_ID})
            check(drain_until(app, "session.removed"), "app_frames.json", "SessionRemoved")
    # The offline projection has `latency_ms: null` and a `last_seen`, so validate it too.
    check(client.get("/api/devices", headers=auth).json(), "http.json", "DeviceListResponse")


def test_terminal_frames_match_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    """A38: the two frames the gateway re-addresses, and the detach it sends on its own account."""
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    terminal_id = "2f8d4b6a-1c3e-4a75-9b0d-6e2f8c4a1d57"
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        check(client.get("/api/devices", headers=auth).json(), "http.json", "DeviceListResponse")
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(
                {
                    "type": "terminal.open",
                    "id": "44444444-5555-4666-8777-888888888888",
                    "device_id": enrolled["device_id"],
                    "cols": 80,
                    "rows": 24,
                }
            )
            forwarded = drain_until(device, "terminal.open")
            check(forwarded, "app_frames.json", "TerminalOpen")
            device.send_json(
                {
                    "type": "reply",
                    "id": forwarded["id"],
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {"terminal_id": terminal_id},
                }
            )
            check(drain_until(app, "reply"), "app_frames.json", "ReplyTerminalOpen")
            device.send_json(
                {
                    "type": "terminal.output",
                    "terminal_id": terminal_id,
                    "to": forwarded["from"],
                    "seq": 1,
                    "data": "JCA=",
                }
            )
            check(drain_until(app, "terminal.output"), "app_frames.json", "TerminalOutput")
            device.send_json(
                {
                    "type": "terminal.exited",
                    "terminal_id": terminal_id,
                    "to": forwarded["from"],
                    "code": 0,
                }
            )
            check(drain_until(app, "terminal.exited"), "app_frames.json", "TerminalExited")
            # And the gateway's own request, once nobody is holding the terminal any more.
            device.send_json(
                {
                    "type": "terminal.output",
                    "terminal_id": terminal_id,
                    "to": "no-such-connection",
                    "seq": 2,
                    "data": "JCA=",
                }
            )
            detach = drain_until(device, "terminal.detach")
            check(detach, "app_frames.json", "TerminalDetach")
            device.send_json(
                {"type": "reply", "id": detach["id"], "from": "gateway", "ok": True, "result": {}}
            )


def test_a_marked_session_and_session_seen_match_the_schema(
    client: TestClient, auth: dict[str, str], web_sender: FakeWebPushSender
) -> None:
    """A47: the marked session wherever an app receives it, `session.seen` and its answer, and
    the push that carries the count."""
    enrolled = enroll_device(client, auth)
    subscribed = client.post(
        "/api/push/web/subscribe",
        json={
            "subscription": {
                "endpoint": "https://push.example.com/subscription/schema",
                "keys": {"p256dh": "A" * 32, "auth": "A" * 32},
            }
        },
        headers=auth,
    )
    assert subscribed.status_code == 200, subscribed.text
    device_id = enrolled["device_id"]
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        running = session_summary(SESSION_ID, device_id, state="running")
        device.send_json(device_hello(sessions=[running]))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            idle = session_summary(SESSION_ID, device_id, state="idle")
            device.send_json({"type": "session.updated", "session": idle})
            marked = drain_until(app, "session.updated")
            assert marked["session"]["unseen"] is True
            check(marked, "app_frames.json", "SessionUpdated")
            check(
                client.get("/api/sessions", headers=auth).json(), "http.json", "SessionListResponse"
            )
            with client.websocket_connect("/ws/app", headers=auth) as other:
                check(drain_until(other, "hello"), "app_frames.json", "Hello")

            app.send_json({"type": "session.subscribe", "id": _uuid(1), "session_id": SESSION_ID})
            check(drain_until(app, "reply"), "app_frames.json", "ReplySessionSubscribe")
            app.send_json({"type": "session.takeover", "id": _uuid(2), "session_id": SESSION_ID})
            forwarded = drain_until(device, "session.takeover")
            device.send_json(
                {
                    "type": "reply",
                    "id": forwarded["id"],
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {"session": idle},
                }
            )
            taken = drain_until(app, "reply")
            assert taken["result"]["session"]["unseen"] is True
            check(taken, "app_frames.json", "ReplySessionTakeover")

            seen = {"type": "session.seen", "id": _uuid(3), "session_id": SESSION_ID}
            check(seen, "app_frames.json", "SessionSeen")
            app.send_json(seen)
            check(drain_until(app, "reply"), "app_frames.json", "Reply")
            cleared = drain_until(app, "session.updated")
            assert "unseen" not in cleared["session"]
            check(cleared, "app_frames.json", "SessionUpdated")
    deadline = time.monotonic() + 2.0
    while not web_sender.sent and time.monotonic() < deadline:
        time.sleep(0.005)
    (_, payload), *_ = web_sender.sent
    assert payload["rc"]["badge"] == 1
    check(payload, "http.json", "PushPayload")


async def test_apns_bodies_match_the_schema(tmp_path: Path) -> None:
    """A47: the alert with its count, and the badge-only notification, as APNs receives them."""
    recorder = ApnsRecorder()
    store = PushStore(tmp_path / "push.sqlite3")
    await store.upsert_apns(
        ApnsRegistration(
            device_token="ab" * 32,
            environment="sandbox",
            bundle_id="com.example.app",
            session_jti="j" * 20,
            username="admin",
            expires_at=9_999_999_999,
        )
    )
    counts = {"admin": 1}

    async def count(username: str) -> int:
        return counts[username]

    async def name(device_id: str) -> str:
        return "mac-studio-office"

    device_id = "c5efb1ec-2912-4619-90f7-93b5172fd712"
    service = PushService(store, device_name=name, badge_count=count, apns=apns_provider(recorder))
    await service.notify(
        "needs_approval", {"device_id": device_id, "session_id": SESSION_ID}, "mac", "admin"
    )
    counts["admin"] = 0
    await service.push_badge(BadgeChange(owner="admin", device_id=device_id, session_id=SESSION_ID))
    await service.stop()
    assert [body["rc"]["kind"] for body in recorder.bodies] == ["needs_approval", "badge"]
    for body in recorder.bodies:
        check(body, "http.json", "PushPayload")


def _uuid(number: int) -> str:
    """A request id, which the schema types as a UUID."""
    return f"00000000-0000-4000-8000-{number:012d}"


def test_pairing_progress_matches_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        enroll_device(client, auth)
        for _ in range(4):
            frame = app.receive_json()
            if frame.get("type") == "pairing.progress":
                check(frame, "app_frames.json", "PairingProgress")
                return
    raise AssertionError("no pairing.progress frame arrived")


def test_account_responses_match_the_schema(client: TestClient, auth: dict[str, str]) -> None:
    """A24: the account objects every app decodes, validated against the normative schema."""
    check(client.get("/api/health").json(), "http.json", "HealthResponse")
    check(client.get("/api/session", headers=auth).json(), "http.json", "AuthSessionResponse")

    created = client.post(
        "/api/users",
        json={"username": "alice", "password": "correct horse battery staple", "role": "member"},
        headers=auth,
    )
    check(created.json(), "http.json", "UserResponse")
    check(client.get("/api/users", headers=auth).json(), "http.json", "UserListResponse")
    check(
        client.patch("/api/users/alice", json={"state": "disabled"}, headers=auth).json(),
        "http.json",
        "UserResponse",
    )
    check(
        client.patch("/api/registration", json={"open": True}, headers=auth).json(),
        "http.json",
        "RegistrationResponse",
    )
    registered = client.post(
        "/api/register",
        json={"username": "bob", "password": "correct horse battery staple"},
        headers={"Origin": "http://testserver"},
    )
    check(registered.json(), "http.json", "LoginResponse")
