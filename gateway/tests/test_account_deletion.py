"""Self-service deletion authenticates the owner and revokes the entire account."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rc_gateway.state import GatewayState

from .conftest import MEMBER_PASSWORD, ORIGIN, add_member, enroll_device


def test_delete_requires_current_password_and_never_accepts_another_username(
    client: TestClient,
    auth: dict[str, str],
    state: GatewayState,
) -> None:
    member = add_member(client, auth, "alice")
    assert (
        client.request(
            "DELETE", "/api/account", headers=member, json={"current_password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.request(
            "DELETE",
            "/api/account",
            headers=member,
            json={"current_password": MEMBER_PASSWORD, "username": "admin"},
        ).status_code
        == 400
    )
    assert (
        client.request(
            "DELETE", "/api/account", headers=auth, json={"current_password": MEMBER_PASSWORD}
        ).status_code
        == 403
    )
    assert client.portal is not None
    assert client.portal.call(state.users.get, "alice") is not None


def test_success_removes_only_own_resources_and_invalidates_existing_session(
    client: TestClient,
    auth: dict[str, str],
    state: GatewayState,
) -> None:
    member = add_member(client, auth, "alice")
    bob = add_member(client, auth, "bob")
    enrolled = enroll_device(client, member)
    other = enroll_device(client, bob)
    response = client.request(
        "DELETE",
        "/api/account",
        headers={**member, "Origin": ORIGIN},
        json={"current_password": MEMBER_PASSWORD},
    )
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert client.get("/api/session", headers=member).status_code == 401
    assert client.get("/api/session", headers=bob).status_code == 200
    assert client.portal is not None
    assert client.portal.call(state.users.get, "alice") is None
    assert client.portal.call(state.devices.get, enrolled["device_id"]) is None
    assert client.portal.call(state.devices.get, other["device_id"]) is not None


def test_cross_origin_deletion_is_refused_even_with_bearer(
    client: TestClient,
    auth: dict[str, str],
) -> None:
    member = add_member(client, auth, "alice")
    response = client.request(
        "DELETE",
        "/api/account",
        headers={**member, "Origin": "https://foreign.example"},
        json={"current_password": MEMBER_PASSWORD},
    )
    assert response.status_code == 403
