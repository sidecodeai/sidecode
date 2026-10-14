"""Accounts: registration, the admin's account routes, and what disabling and deleting reach.

A24. The gateway had one password and one user; it now has a table of them, and everything an
account owns — devices, the sessions on them, pairing codes, push registrations and login
sessions — has to follow the account when it is disabled or deleted.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.accounts import hash_password_sync, verify_hash_sync
from rc_gateway.devices import DeviceStore
from rc_gateway.push_store import ApnsRegistration, PushStore, WebPushSubscription
from rc_gateway.state import GatewayState
from rc_gateway.users import UserStore

from .conftest import (
    FIXTURE_DIR,
    MEMBER_PASSWORD,
    ORIGIN,
    PASSWORD,
    add_member,
    device_hello,
    drain_until,
    enroll_device,
    session_summary,
    sign_in,
)

SESSION_ID = "eeee1111-2222-4333-8444-555555555555"
INVALID_DIR = FIXTURE_DIR.parent / "fixtures_invalid"


def fixture(*parts: str) -> Any:
    return json.loads(FIXTURE_DIR.joinpath(*parts).read_text(encoding="utf-8"))


def _open_registration(client: TestClient, auth: dict[str, str]) -> None:
    response = client.patch("/api/registration", json={"open": True}, headers=auth)
    assert response.status_code == 200, response.text
    assert response.json() == {"open": True}


def _register(client: TestClient, username: str, password: str = MEMBER_PASSWORD) -> Any:
    return client.post(
        "/api/register",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN},
    )


# ---- password rules and hashing ----


def test_a_password_hash_round_trips_and_carries_its_parameters() -> None:
    encoded = hash_password_sync("correct horse battery staple")
    assert encoded.startswith("scrypt$16384$8$1$")
    assert verify_hash_sync("correct horse battery staple", encoded) is True
    assert verify_hash_sync("wrong", encoded) is False
    # Two hashes of one password differ: the salt is per row.
    assert encoded != hash_password_sync("correct horse battery staple")
    assert verify_hash_sync("anything", None) is False
    assert verify_hash_sync("anything", "not-a-hash") is False


# ---- registration ----


def test_registration_is_closed_until_the_admin_opens_it(
    client: TestClient, auth: dict[str, str]
) -> None:
    assert client.get("/api/health").json()["auth"]["registration_open"] is False
    closed = _register(client, "alice")
    assert closed.status_code == 403
    assert closed.json()["error"]["code"] == "forbidden"

    _open_registration(client, auth)
    assert client.get("/api/health").json()["auth"]["registration_open"] is True

    opened = _register(client, "alice")
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["user"] == {"username": "alice", "role": "member"}
    assert body["token"]
    # Registration signs the new account in, so the token it returns is already usable.
    session = client.get("/api/session", headers={"Authorization": f"Bearer {body['token']}"})
    assert session.json()["user"] == {"username": "alice", "role": "member"}


def test_registration_refuses_a_taken_name_and_a_name_outside_the_rules(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    _open_registration(client, auth)
    assert _register(client, "alice").status_code == 200
    state.login_limiter.reset()
    assert _register(client, "ALICE").status_code == 409
    assert _register(client, "admin").status_code == 409
    assert _register(client, "no").status_code == 400
    assert _register(client, "Bad Name").status_code == 400
    state.login_limiter.reset()
    assert _register(client, "bob", password="short").status_code == 400


def test_registration_is_rate_limited_and_checks_the_origin(
    client: TestClient, auth: dict[str, str]
) -> None:
    _open_registration(client, auth)
    foreign = client.post(
        "/api/register",
        json={"username": "alice", "password": MEMBER_PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    for index in range(5):
        _register(client, f"user{index}")
    assert _register(client, "another").status_code == 429


# ---- the caller's own password ----


def test_a_member_changes_its_own_password_and_the_admin_cannot(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "alice")
    wrong = client.post(
        "/api/password",
        json={"current_password": "not it", "new_password": "a new long password"},
        headers=member,
    )
    assert wrong.status_code == 401

    short = client.post(
        "/api/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "short"},
        headers=member,
    )
    assert short.status_code == 400

    changed = client.post(
        "/api/password",
        json={"current_password": MEMBER_PASSWORD, "new_password": "a new long password"},
        headers=member,
    )
    assert changed.status_code == 200
    # The old sign-in stays valid; the new password is what signs in from now on.
    assert client.get("/api/session", headers=member).status_code == 200
    assert sign_in(client, "alice", "a new long password")

    refused = client.post(
        "/api/password",
        json={"current_password": PASSWORD, "new_password": "a new long password"},
        headers=auth,
    )
    assert refused.status_code == 403


# ---- the admin's account routes ----


def test_only_the_admin_reaches_the_account_routes(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "alice")
    for method, path in (
        ("GET", "/api/users"),
        ("POST", "/api/users"),
        ("PATCH", "/api/users/admin"),
        ("DELETE", "/api/users/admin"),
        ("PATCH", "/api/registration"),
    ):
        response = client.request(method, path, json={}, headers=member)
        assert response.status_code == 403, f"{method} {path}"
        assert response.json()["error"]["code"] == "forbidden"
    assert client.get("/api/users", headers=auth).status_code == 200


def test_the_account_list_carries_roles_states_and_device_counts(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "alice")
    enroll_device(client, auth, name="admin-mac")
    enroll_device(client, auth, name="admin-laptop")
    enroll_device(client, member, name="alice-mac")

    body = client.get("/api/users", headers=auth).json()
    assert body["registration_open"] is False
    listed = {item["username"]: item for item in body["users"]}
    assert [item["username"] for item in body["users"]] == ["admin", "alice"]
    assert listed["admin"]["role"] == "admin"
    assert listed["admin"]["devices"] == 2
    assert listed["alice"] == {
        "username": "alice",
        "role": "member",
        "state": "active",
        "created_at": listed["alice"]["created_at"],
        "last_login_at": listed["alice"]["last_login_at"],
        "devices": 1,
    }
    assert listed["alice"]["last_login_at"] > 0
    assert "password_hash" not in listed["alice"]


def test_the_admin_creates_resets_promotes_and_refuses_duplicates(
    client: TestClient, auth: dict[str, str]
) -> None:
    created = client.post(
        "/api/users",
        json={"username": "alice", "password": MEMBER_PASSWORD, "role": "member"},
        headers=auth,
    )
    assert created.status_code == 200, created.text
    assert created.json()["user"]["role"] == "member"
    assert created.json()["user"]["last_login_at"] is None

    assert (
        client.post(
            "/api/users", json={"username": "alice", "password": MEMBER_PASSWORD}, headers=auth
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/users",
            json={"username": "bob", "password": MEMBER_PASSWORD, "role": "owner"},
            headers=auth,
        ).status_code
        == 400
    )

    reset = client.patch("/api/users/alice", json={"password": "a reset password"}, headers=auth)
    assert reset.status_code == 200
    assert sign_in(client, "alice", "a reset password")

    promoted = client.patch("/api/users/alice", json={"role": "admin"}, headers=auth)
    assert promoted.json()["user"]["role"] == "admin"
    assert client.patch("/api/users/alice", json={}, headers=auth).status_code == 400
    unknown = client.patch("/api/users/nobody", json={"role": "admin"}, headers=auth)
    assert unknown.status_code == 404


def test_the_admin_account_cannot_be_disabled_demoted_or_deleted(
    client: TestClient, auth: dict[str, str]
) -> None:
    for body in ({"state": "disabled"}, {"role": "member"}, {"password": "a long enough one"}):
        response = client.patch("/api/users/admin", json=body, headers=auth)
        assert response.status_code == 409, body
        assert response.json()["error"]["code"] == "conflict"
    deleted = client.delete("/api/users/admin", headers=auth)
    assert deleted.status_code == 409
    assert client.get("/api/session", headers=auth).status_code == 200


# ---- disabling ----


def test_disabling_an_account_signs_it_out_everywhere_and_refuses_its_devices(
    client: TestClient, auth: dict[str, str]
) -> None:
    from starlette.websockets import WebSocketDisconnect

    member = add_member(client, auth, "alice")
    enrolled = enroll_device(client, member, name="alice-mac")
    device_headers = {"Authorization": f"Bearer {enrolled['device_token']}"}

    with client.websocket_connect("/ws/device", headers=device_headers) as device:
        device.send_json(device_hello())
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=member) as app:
            drain_until(app, "hello")
            patched = client.patch("/api/users/alice", json={"state": "disabled"}, headers=auth)
            assert patched.status_code == 200
            assert patched.json()["user"]["state"] == "disabled"
            with pytest.raises(WebSocketDisconnect) as app_closed:
                for _ in range(5):
                    app.receive_json()
        with pytest.raises(WebSocketDisconnect) as device_closed:
            for _ in range(5):
                device.receive_json()
    assert app_closed.value.code == 4401
    assert device_closed.value.code == 4403

    assert client.get("/api/session", headers=member).status_code == 401
    refused = client.post(
        "/api/login",
        json={"username": "alice", "password": MEMBER_PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"


def test_a_disabled_accounts_device_cannot_reconnect_until_it_is_enabled(
    client: TestClient, auth: dict[str, str]
) -> None:
    from .conftest import close_code_for

    member = add_member(client, auth, "alice")
    enrolled = enroll_device(client, member, name="alice-mac")
    device_headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    disabled = client.patch("/api/users/alice", json={"state": "disabled"}, headers=auth)
    assert disabled.status_code == 200
    assert close_code_for(client, "/ws/device", headers=device_headers) == 4403

    enabled = client.patch("/api/users/alice", json={"state": "active"}, headers=auth)
    assert enabled.status_code == 200
    with client.websocket_connect("/ws/device", headers=device_headers) as device:
        device.send_json(device_hello())
        assert device.receive_json()["type"] == "hello_ack"
    assert sign_in(client, "alice", MEMBER_PASSWORD)


# ---- deleting ----


def test_deleting_an_account_takes_its_devices_sessions_and_registrations(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    member = add_member(client, auth, "alice")
    enrolled = enroll_device(client, member, name="alice-mac")
    device_headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    client.post(
        "/api/push/web/subscribe",
        json={
            "subscription": {
                "endpoint": "https://push.example.com/alice",
                "keys": {"p256dh": "A" * 32, "auth": "B" * 32},
            }
        },
        headers=member,
    )
    client.post("/api/devices/pairing", headers=member)

    from starlette.websockets import WebSocketDisconnect

    from .conftest import close_code_for

    with client.websocket_connect("/ws/device", headers=device_headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=member) as app:
            drain_until(app, "hello")
            assert client.delete("/api/users/alice", headers=auth).status_code == 200
            # The owner's own socket is told what left before it is signed out.
            assert drain_until(app, "session.removed")["session_id"] == SESSION_ID
            assert drain_until(app, "device.removed")["device_id"] == enrolled["device_id"]
            with pytest.raises(WebSocketDisconnect) as app_closed:
                for _ in range(5):
                    app.receive_json()
        with pytest.raises(WebSocketDisconnect):
            for _ in range(5):
                device.receive_json()
    assert app_closed.value.code == 4401

    assert client.get("/api/session", headers=member).status_code == 401
    assert leftover_rows(state) == ([], [], 0)
    listed = client.get("/api/users", headers=auth).json()["users"]
    assert [item["username"] for item in listed] == ["admin"]
    # The device token stops working, and the session left the index with the device.
    assert close_code_for(client, "/ws/device", headers=device_headers) == 4401
    assert client.get("/api/sessions", headers=auth).json()["sessions"] == []
    assert client.delete("/api/users/alice", headers=auth).status_code == 404


def leftover_rows(state: GatewayState) -> tuple[list[Any], list[Any], int]:
    """What the deleted account left behind: web push, APNs and pairing rows."""
    import asyncio

    async def read() -> tuple[list[Any], list[Any], int]:
        with sqlite3.connect(state.devices.path) as connection:
            pairings = int(
                connection.execute(
                    "SELECT COUNT(*) FROM pairing_codes WHERE username='alice'"
                ).fetchone()[0]
            )
        return (
            await state.push_store.list_web("alice"),
            await state.push_store.list_apns("alice"),
            pairings,
        )

    return asyncio.run(read())


# ---- migrations from before accounts existed ----


def test_stores_written_before_accounts_open_and_migrate(tmp_path: Path) -> None:
    """The shapes a gateway deployed before A24 left on disk (`push.sqlite3` has no username)."""
    push_path = tmp_path / "push.sqlite3"
    with sqlite3.connect(push_path) as connection:
        connection.execute(
            "CREATE TABLE web_push (endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, "
            "auth TEXT NOT NULL, session_jti TEXT NOT NULL, expires_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE apns_tokens (device_token TEXT PRIMARY KEY, environment TEXT NOT NULL, "
            "bundle_id TEXT NOT NULL, session_jti TEXT NOT NULL, expires_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO web_push VALUES ('https://push.example.com/x', 'k', 'a', 'j', "
            "9999999999, 0)"
        )
        connection.execute(
            "INSERT INTO apns_tokens VALUES ('ab', 'sandbox', 'com.example', 'j', 9999999999, 0)"
        )

    devices_path = tmp_path / "devices.sqlite3"
    with sqlite3.connect(devices_path) as connection:
        connection.execute(
            "CREATE TABLE devices (device_id TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "name TEXT NOT NULL, platform TEXT NOT NULL, hostname TEXT NOT NULL, "
            "arch TEXT NOT NULL, client_version TEXT NOT NULL, token_hash BLOB NOT NULL UNIQUE, "
            "created_at INTEGER NOT NULL, last_seen INTEGER)"
        )
        connection.execute(
            "CREATE TABLE pairing_codes (code_hash BLOB PRIMARY KEY, username TEXT NOT NULL, "
            "created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO devices VALUES ('d1', 'admin', 'mac', 'macos', 'h', 'arm64', '0.1.0', "
            "x'00', 1, NULL)"
        )

    import asyncio

    store = PushStore(push_path)
    devices = DeviceStore(devices_path)

    async def read() -> tuple[list[WebPushSubscription], list[ApnsRegistration], Any]:
        return (
            await store.list_web("admin"),
            await store.list_apns("admin"),
            await devices.get("d1"),
        )

    web, apns, record = asyncio.run(read())
    # Rows from before accounts existed belong to the only account there was.
    assert [item.username for item in web] == ["admin"]
    assert [item.username for item in apns] == ["admin"]
    assert record is not None
    assert record.username == "admin"
    assert record.update_state == "idle"


def test_a_fresh_user_store_has_the_admin_and_closed_registration(tmp_path: Path) -> None:
    import asyncio

    store = UserStore(tmp_path / "users.sqlite3")

    async def read() -> tuple[Any, bool]:
        return await store.get("admin"), await store.registration_open()

    admin, open_ = asyncio.run(read())
    assert admin is not None
    assert admin.role == "admin"
    assert admin.state == "active"
    # The operator's password is RC_PASSWORD and is never written to this file.
    assert admin.password_hash is None
    assert open_ is False


# ---- the protocol's fixtures ----


@pytest.mark.skipif(not FIXTURE_DIR.is_dir(), reason="protocol/fixtures is not present")
def test_every_account_fixture_moves_through_the_real_routes(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    assert client.patch(
        "/api/registration", json=fixture("http", "registration.patch.request.json"), headers=auth
    ).json() == fixture("http", "registration.response.json")

    registered = client.post(
        "/api/register", json=fixture("http", "register.request.json"), headers={"Origin": ORIGIN}
    )
    assert registered.status_code == 200, registered.text
    assert set(registered.json()) == set(fixture("http", "login.response.json"))

    session = client.get(
        "/api/session", headers={"Authorization": f"Bearer {registered.json()['token']}"}
    )
    assert set(session.json()) == set(fixture("http", "auth.session.response.json"))

    changed = client.post(
        "/api/password",
        json=fixture("http", "password.request.json"),
        headers={"Authorization": f"Bearer {registered.json()['token']}"},
    )
    assert changed.status_code == 200

    created = client.post(
        "/api/users", json=fixture("http", "users.create.request.json"), headers=auth
    )
    assert created.status_code == 409, "the fixture registers the same username"

    listed = client.get("/api/users", headers=auth).json()
    expected_list = fixture("http", "users.list.response.json")
    assert set(listed) == set(expected_list)
    assert set(listed["users"][0]) == set(expected_list["users"][0])

    patched = client.patch(
        "/api/users/alice", json=fixture("http", "users.patch.request.json"), headers=auth
    )
    assert set(patched.json()) == set(fixture("http", "users.response.json"))
    assert patched.json()["user"]["state"] == "disabled"

    health = client.get("/api/health").json()
    assert set(health["auth"]) == set(fixture("http", "health.response.json")["auth"])


@pytest.mark.skipif(not INVALID_DIR.is_dir(), reason="protocol/fixtures_invalid is not present")
def test_the_invalid_login_fixture_is_refused(client: TestClient) -> None:
    body = json.loads((INVALID_DIR / "http.login__no_username.json").read_text(encoding="utf-8"))
    response = client.post("/api/login", json=body, headers={"Origin": ORIGIN})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
