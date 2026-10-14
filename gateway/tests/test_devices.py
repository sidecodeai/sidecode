"""Pairing, enrollment, device WebSocket authentication and revocation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from rc_gateway.devices import (
    DeviceStore,
    generate_pairing_code,
    normalize_pairing_code,
)
from rc_gateway.state import GatewayState

from .conftest import (
    ORIGIN,
    add_member,
    close_code_for,
    device_hello,
    drain_until,
    enroll_device,
)


def test_pairing_code_format() -> None:
    code = generate_pairing_code()
    assert code.startswith("RC-")
    assert len(code) == 12 and code[7] == "-"
    assert not set(code[3:7] + code[8:]) & set("ILOU")
    assert normalize_pairing_code(code.lower()) == normalize_pairing_code(code)


def test_pairing_returns_a_copyable_install_command(
    client: TestClient, auth: dict[str, str]
) -> None:
    body = client.post("/api/devices/pairing", headers=auth).json()
    assert body["code"] in body["install"]["macos"]
    assert body["install"]["macos"] == body["install"]["linux"]
    assert body["install"]["macos"].startswith(f"curl -fsSL {ORIGIN}/install.sh")
    assert body["expires_at"] > 0


def test_enroll_then_revoke(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    assert enrolled["gateway_ws_url"] == "ws://testserver/ws/device"
    assert len(enrolled["device_token"]) >= 32

    listed = client.get("/api/devices", headers=auth).json()["devices"]
    assert [item["device_id"] for item in listed] == [enrolled["device_id"]]
    assert listed[0]["online"] is False

    removed = client.delete(f"/api/devices/{enrolled['device_id']}", headers=auth)
    assert removed.status_code == 200
    assert client.get("/api/devices", headers=auth).json()["devices"] == []


def test_a_pairing_code_is_single_use(client: TestClient, auth: dict[str, str]) -> None:
    code = client.post("/api/devices/pairing", headers=auth).json()["code"]
    payload = {
        "code": code,
        "name": "one",
        "platform": "linux",
        "hostname": "box",
        "arch": "x86_64",
        "client_version": "0.1.0",
    }
    assert client.post("/api/devices/enroll", json=payload).status_code == 200
    # A spent code stays on file until it expires, so a replay is a conflict rather than being
    # indistinguishable from a code that never existed.
    replayed = client.post("/api/devices/enroll", json=payload)
    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "conflict"


def test_an_unknown_code_is_not_found(client: TestClient) -> None:
    response = client.post(
        "/api/devices/enroll",
        json={
            "code": "RC-ZZZZ-ZZZZ",
            "name": "x",
            "platform": "linux",
            "hostname": "b",
            "arch": "x86_64",
            "client_version": "0.1.0",
        },
    )
    assert response.status_code == 404


def test_cancelling_a_code_stops_enrollment(client: TestClient, auth: dict[str, str]) -> None:
    code = client.post("/api/devices/pairing", headers=auth).json()["code"]
    assert client.delete(f"/api/devices/pairing/{code}", headers=auth).status_code == 200
    response = client.post(
        "/api/devices/enroll",
        json={
            "code": code,
            "name": "x",
            "platform": "linux",
            "hostname": "b",
            "arch": "x86_64",
            "client_version": "0.1.0",
        },
    )
    assert response.status_code == 404


def test_rename_broadcasts_to_apps(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        renamed = client.patch(
            f"/api/devices/{enrolled['device_id']}", json={"name": "office"}, headers=auth
        )
        assert renamed.status_code == 200
        assert renamed.json()["device"]["name"] == "office"
        pushed = drain_until(app, "device.updated")
        assert pushed["device"]["name"] == "office"


def test_device_socket_rejects_a_bad_token(client: TestClient) -> None:
    assert (
        close_code_for(client, "/ws/device", headers={"Authorization": "Bearer " + "z" * 64})
        == 4401
    )


def test_device_hello_registers_and_announces(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            ack = device.receive_json()
            assert ack["type"] == "hello_ack"
            assert ack["device_id"] == enrolled["device_id"]
            assert ack["config"] == {"delta_flush_ms": 80, "max_event_bytes": 65536}

            pushed = drain_until(app, "device.updated")
            assert pushed["device"]["online"] is True
            assert pushed["device"]["agents"][0]["agent"] == "claude"
            assert state.hub.device_online(enrolled["device_id"])

        # A13: the socket is gone, but a dropped link is not an offline device until the grace
        # period ends without a replacement.
        during_grace = client.get("/api/devices", headers=auth).json()["devices"][0]
        assert during_grace["online"] is True
        offline = drain_until(app, "device.updated")
        assert offline["device"]["online"] is False

    listed = client.get("/api/devices", headers=auth).json()["devices"][0]
    assert listed["agents"][0]["agent"] == "claude"
    assert listed["online"] is False


def test_a_second_connection_replaces_the_first(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as first:
        first.send_json(device_hello())
        assert first.receive_json()["type"] == "hello_ack"
        with client.websocket_connect("/ws/device", headers=headers) as second:
            second.send_json(device_hello())
            assert second.receive_json()["type"] == "hello_ack"
            with pytest.raises(WebSocketDisconnect):
                for _ in range(5):
                    first.receive_json()


def test_revoking_a_device_closes_its_socket(client: TestClient, auth: dict[str, str]) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello())
        assert device.receive_json()["type"] == "hello_ack"
        assert (
            client.delete(f"/api/devices/{enrolled['device_id']}", headers=auth).status_code == 200
        )
        with pytest.raises(WebSocketDisconnect):
            for _ in range(5):
                device.receive_json()


@pytest.mark.asyncio
async def test_store_caps_active_devices(tmp_path_factory: pytest.TempPathFactory) -> None:
    store = DeviceStore(tmp_path_factory.mktemp("devices") / "devices.sqlite3")
    for index in range(64):
        grant = await store.create_pairing("admin")
        result = await store.redeem(
            grant.code,
            name=f"d{index}",
            platform="linux",
            hostname="h",
            arch="x86_64",
            client_version="0.1.0",
        )
        assert not isinstance(result, str)
    grant = await store.create_pairing("admin")
    assert (
        await store.redeem(
            grant.code,
            name="over",
            platform="linux",
            hostname="h",
            arch="x86_64",
            client_version="0.1.0",
        )
        == "conflict"
    )


@pytest.mark.asyncio
async def test_expired_codes_are_not_redeemable(tmp_path_factory: pytest.TempPathFactory) -> None:
    store = DeviceStore(tmp_path_factory.mktemp("devices") / "devices.sqlite3")
    grant = await store.create_pairing("admin", ttl=10, now=1000)
    outcome = await store.redeem(
        grant.code,
        name="late",
        platform="linux",
        hostname="h",
        arch="x86_64",
        client_version="0.1.0",
        now=2000,
    )
    assert outcome == "not_found"


@pytest.mark.asyncio
async def test_a_database_from_an_older_release_is_migrated(tmp_path: Path) -> None:
    """A column added to CREATE TABLE IF NOT EXISTS never reaches an existing DATA_DIR."""
    path = tmp_path / "devices.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY, username TEXT NOT NULL, name TEXT NOT NULL,
            platform TEXT NOT NULL, hostname TEXT NOT NULL, arch TEXT NOT NULL,
            client_version TEXT NOT NULL, token_hash BLOB NOT NULL UNIQUE,
            created_at INTEGER NOT NULL, last_seen INTEGER);
        CREATE TABLE pairing_codes (
            code_hash BLOB PRIMARY KEY, username TEXT NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        INSERT INTO devices VALUES
            ('legacy-1','admin','old-box','linux','h','x86_64','0.0.9',X'00',1,NULL);
        """
    )
    legacy.commit()
    legacy.close()

    store = DeviceStore(path)
    grant = await store.create_pairing("admin")
    enrolled = await store.redeem(
        grant.code,
        name="new",
        platform="linux",
        hostname="h",
        arch="x86_64",
        client_version="0.1.0",
    )
    assert not isinstance(enrolled, str), enrolled
    # The pre-existing row survives and reads back with the new column's default.
    listed = {record.device_id: record for record in await store.list_for_user("admin")}
    assert listed["legacy-1"].name == "old-box"
    assert listed["legacy-1"].agents == []
    # A36's column is one of them: an upgraded gateway reads a row that never had it.
    assert listed["legacy-1"].update_failed_build is None
    # A38's likewise, and NULL there means "this client never said", not "no shell".
    assert listed["legacy-1"].terminal is None
    # And the column that the migration added is now usable.
    replayed = await store.redeem(
        grant.code,
        name="again",
        platform="linux",
        hostname="h",
        arch="x86_64",
        client_version="0.1.0",
    )
    assert replayed == "conflict"


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "devices.sqlite3"
    first = DeviceStore(path)
    second = DeviceStore(path)
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(pairing_codes)")]
    assert columns.count("redeemed_at") == 1
    assert first.path == second.path


def test_cancelling_another_account_pairing_leaves_its_progress_alone(
    client: TestClient, auth: dict[str, str]
) -> None:
    """GW-11: the hub's pairing map has no account column, so the 404 has to come first.

    The damage is to the steps after enrolment: clearing the entry leaves `_advance_pairing` with
    nothing to match the device against, so `online` and `agents` never reach the owner and their
    pairing screen sits on "waiting" even though the device is up.
    """
    mallory = add_member(client, auth, "mallory")
    code = client.post("/api/devices/pairing", headers=auth).json()["code"]
    enrolled = client.post(
        "/api/devices/enroll", json={"code": code, "name": "box", "platform": "linux"}
    )
    assert enrolled.status_code == 200, enrolled.text

    assert client.delete(f"/api/devices/pairing/{code}", headers=mallory).status_code == 404

    device_headers = {"Authorization": f"Bearer {enrolled.json()['device_token']}"}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=device_headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            steps = []
            for _ in range(8):
                frame = app.receive_json()
                if frame.get("type") == "pairing.progress":
                    steps.append(frame["step"])
                if "agents" in steps:
                    break
    assert steps == ["online", "agents"]
