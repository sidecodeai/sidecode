"""Notification triggers, suppression while an app is watching, and delivery bookkeeping."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.apns import ApnsProvider, ApnsResponse
from rc_gateway.push import (
    KIND_ERROR,
    KIND_NEEDS_APPROVAL,
    KIND_NEEDS_INPUT,
    KIND_TURN_COMPLETED,
    WEB_PUSH_TIMEOUT_SECONDS,
    PushService,
    transition_kind,
)
from rc_gateway.push_store import ApnsRegistration, PushStore, WebPushSubscription
from rc_gateway.state import GatewayState

from .conftest import (
    FakeWebPushSender,
    add_member,
    device_hello,
    drain_until,
    enroll_device,
    no_unseen,
    session_summary,
)

SESSION_ID = "99999999-8888-7777-6666-555555555555"
ENDPOINT = "https://push.example.com/subscription/abc"
KEY = "A" * 32


def _subscribe(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/api/push/web/subscribe",
        json={"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": KEY, "auth": KEY}}},
        headers=auth,
    )
    assert response.status_code == 200, response.text


def _transition(device: Any, device_id: str, state: str) -> None:
    device.send_json(
        {
            "type": "session.updated",
            "session": session_summary(SESSION_ID, device_id, state=state),
        }
    )


def _transition_and_settle(
    device: Any, app: Any, device_id: str, state: str, *, sender: FakeWebPushSender, pushes: int
) -> None:
    """Apply a state change and wait until the gateway has finished reacting to it.

    The device loop broadcasts ``session.updated`` before it evaluates the push trigger, so a
    repeated no-op update whose broadcast the app has seen proves the previous frame's handler ran
    to completion. Delivery deliberately is not part of that handler, so it would hold up the next
    frame: how many notifications the run should have produced by now is waited for separately.
    """
    _transition(device, device_id, state)
    drain_until(app, "session.updated")
    _transition(device, device_id, state)
    drain_until(app, "session.updated")
    deadline = time.monotonic() + 2.0
    while len(sender.sent) < pushes and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(sender.sent) == pushes, [payload["rc"]["kind"] for _, payload in sender.sent]


def test_transition_kinds() -> None:
    assert transition_kind("idle", "needs_approval") == KIND_NEEDS_APPROVAL
    assert transition_kind("running", "needs_input") == KIND_NEEDS_INPUT
    assert transition_kind("running", "idle") == KIND_TURN_COMPLETED
    assert transition_kind("needs_approval", "idle") == KIND_TURN_COMPLETED
    assert transition_kind("running", "error") == KIND_ERROR
    assert transition_kind("idle", "running") is None
    assert transition_kind("idle", "idle") is None
    assert transition_kind("stopped", "idle") is None


def test_vapid_key_is_exposed(client: TestClient, auth: dict[str, str]) -> None:
    body = client.get("/api/push/web/vapid", headers=auth).json()
    assert body["public_key"].startswith("BJ")


def test_subscription_is_validated(client: TestClient, auth: dict[str, str]) -> None:
    bad = client.post(
        "/api/push/web/subscribe",
        json={"subscription": {"endpoint": "http://insecure.example", "keys": {}}},
        headers=auth,
    )
    assert bad.status_code == 400


def test_state_transitions_produce_one_push_each(
    client: TestClient, auth: dict[str, str], web_sender: FakeWebPushSender
) -> None:
    enrolled = enroll_device(client, auth)
    _subscribe(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            _transition_and_settle(
                device, app, enrolled["device_id"], "running", sender=web_sender, pushes=0
            )
            _transition_and_settle(
                device, app, enrolled["device_id"], "needs_approval", sender=web_sender, pushes=1
            )
            _transition_and_settle(
                device, app, enrolled["device_id"], "idle", sender=web_sender, pushes=2
            )

    kinds = [payload["rc"]["kind"] for _, payload in web_sender.sent]
    assert kinds == [KIND_NEEDS_APPROVAL, KIND_TURN_COMPLETED]
    endpoint, payload = web_sender.sent[0]
    assert endpoint == ENDPOINT
    assert payload["rc"]["device_name"] == "mac-studio"
    assert payload["rc"]["session_id"] == SESSION_ID
    assert payload["rc"]["title"] == "mac-studio: approval needed"
    assert set(payload) == {"rc"}


def test_no_push_while_a_subscribed_app_is_active(
    client: TestClient, auth: dict[str, str], web_sender: FakeWebPushSender
) -> None:
    enrolled = enroll_device(client, auth)
    _subscribe(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "p1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            _transition_and_settle(
                device, app, enrolled["device_id"], "needs_approval", sender=web_sender, pushes=0
            )
    assert web_sender.sent == []


def test_a_gone_subscription_is_dropped(
    client: TestClient, auth: dict[str, str], web_sender: FakeWebPushSender, state: GatewayState
) -> None:
    enrolled = enroll_device(client, auth)
    _subscribe(client, auth)
    web_sender.status = 410
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            _transition_and_settle(
                device, app, enrolled["device_id"], "error", sender=web_sender, pushes=1
            )
    assert len(web_sender.sent) == 1
    remaining = client.post(
        "/api/push/web/subscribe",
        json={"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": KEY, "auth": KEY}}},
        headers=auth,
    )
    assert remaining.status_code == 200


def test_logout_purges_registrations(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    _subscribe(client, auth)
    assert client.post("/api/logout", headers=auth).status_code == 200
    assert asyncio.run(state.push_store.list_web("admin")) == []


def test_apns_registration_requires_configuration(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/api/push/apns/register",
        json={"token": "ab" * 32, "environment": "sandbox", "bundle_id": "com.example.app"},
        headers=auth,
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unsupported"


@pytest.mark.asyncio
async def test_apns_journal_retries_then_gives_up(tmp_path: Path) -> None:
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
    responses: list[ApnsResponse] = [
        ApnsResponse(503, "ServiceUnavailable"),
        ApnsResponse(200),
    ]
    sent: list[bytes] = []

    async def sender(url: str, headers: dict[str, str], payload: bytes) -> ApnsResponse:
        sent.append(payload)
        return responses.pop(0)

    provider = ApnsProvider(
        "ABCDE12345",
        "FGHIJ67890",
        "com.example.app",
        _p256_pem(),
        environment="sandbox",
        sender=sender,
    )
    service = PushService(store, device_name=_name, badge_count=no_unseen, apns=provider)
    await service.notify(KIND_NEEDS_APPROVAL, {"device_id": "d", "session_id": "s"}, "mac", "admin")
    assert await store.pending_count() == 1
    body = json.loads(sent[0])
    assert body["aps"]["alert"] == {"title": "SideCode", "body": "mac: approval needed"}
    assert body["rc"]["kind"] == KIND_NEEDS_APPROVAL

    await store.retry_at(1, 0.0)
    await service.flush_apns()
    assert await store.pending_count() == 0


@pytest.mark.asyncio
async def test_a_dead_apns_token_is_deleted(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.sqlite3")
    await store.upsert_apns(
        ApnsRegistration(
            device_token="cd" * 32,
            environment="sandbox",
            bundle_id="com.example.app",
            session_jti="j" * 20,
            username="admin",
            expires_at=9_999_999_999,
        )
    )

    async def sender(url: str, headers: dict[str, str], payload: bytes) -> ApnsResponse:
        return ApnsResponse(410, "Unregistered")

    provider = ApnsProvider(
        "ABCDE12345",
        "FGHIJ67890",
        "com.example.app",
        _p256_pem(),
        environment="sandbox",
        sender=sender,
    )
    service = PushService(store, device_name=_name, badge_count=no_unseen, apns=provider)
    await service.notify(KIND_ERROR, {"device_id": "d", "session_id": "s"}, "mac", "admin")
    assert await store.list_apns("admin") == []
    assert await store.pending_count() == 0


async def _name(device_id: str) -> str:
    return "mac"


def _p256_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_a_push_registration_never_changes_account(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    """GW-2: re-registering someone else's endpoint must not move their notifications."""
    mallory = add_member(client, auth, "mallory")
    _subscribe(client, auth)

    stolen = client.post(
        "/api/push/web/subscribe",
        json={"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": KEY, "auth": KEY}}},
        headers=mallory,
    )
    assert stolen.status_code == 409
    assert stolen.json()["error"]["code"] == "conflict"

    owned = asyncio.run(state.push_store.list_web("admin"))
    assert [item.endpoint for item in owned] == [ENDPOINT]
    assert asyncio.run(state.push_store.list_web("mallory")) == []


def test_a_push_registration_is_only_deleted_by_its_owner(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    """GW-2: the DELETE routes are keyed by endpoint, so they have to filter on the account."""
    mallory = add_member(client, auth, "mallory")
    _subscribe(client, auth)

    refused = client.request(
        "DELETE", "/api/push/web/subscribe", json={"endpoint": ENDPOINT}, headers=mallory
    )
    assert refused.status_code == 200
    assert [item.endpoint for item in asyncio.run(state.push_store.list_web("admin"))] == [ENDPOINT]

    mine = client.request(
        "DELETE", "/api/push/web/subscribe", json={"endpoint": ENDPOINT}, headers=auth
    )
    assert mine.status_code == 200
    assert asyncio.run(state.push_store.list_web("admin")) == []


@pytest.mark.asyncio
async def test_an_apns_token_is_bound_to_one_account(tmp_path: Path) -> None:
    """GW-2: the same rule on the APNs table, whose delete also drops queued deliveries."""
    store = PushStore(tmp_path / "push.sqlite3")
    registration = ApnsRegistration(
        device_token="ef" * 32,
        environment="sandbox",
        bundle_id="com.example.app",
        session_jti="j" * 20,
        username="admin",
        expires_at=9_999_999_999,
    )
    assert await store.upsert_apns(registration)
    await store.enqueue(registration.device_token, "sandbox", "{}")

    from dataclasses import replace

    assert not await store.upsert_apns(replace(registration, username="mallory"))
    assert [item.username for item in await store.list_apns("admin")] == ["admin"]
    assert await store.list_apns("mallory") == []

    await store.remove_apns(registration.device_token, "mallory")
    assert len(await store.list_apns("admin")) == 1
    assert await store.pending_count() == 1

    await store.remove_apns(registration.device_token, "admin")
    assert await store.list_apns("admin") == []
    assert await store.pending_count() == 0


@pytest.mark.asyncio
async def test_web_push_runs_on_its_own_threads_with_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GW-3: `pywebpush` never applies its own default timeout, and it must not use the loop's
    default executor, which every blocking database call in the gateway shares."""
    import pywebpush

    seen: dict[str, Any] = {}

    def fake_webpush(**kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["thread"] = threading.current_thread().name
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    service = PushService(
        PushStore(tmp_path / "push.sqlite3"),
        device_name=_name,
        badge_count=no_unseen,
        vapid_private_key="key",
        vapid_contact="mailto:admin@example.com",
    )
    status = await service._send_with_pywebpush(_web_subscription(), "{}")
    await service.stop()

    assert status == 201
    assert seen["timeout"] == WEB_PUSH_TIMEOUT_SECONDS
    assert seen["thread"].startswith("rc-push")


@pytest.mark.asyncio
async def test_a_hanging_web_push_does_not_block_a_database_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GW-3: the default pool is two threads here, as it is six on two CPUs."""
    import pywebpush

    released = threading.Event()

    def fake_webpush(**kwargs: Any) -> Any:
        released.wait(timeout=10)
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rc-default")
    loop.set_default_executor(default)
    store = PushStore(tmp_path / "push.sqlite3")
    service = PushService(
        store,
        device_name=_name,
        badge_count=no_unseen,
        vapid_private_key="key",
        vapid_contact="mailto:admin@example.com",
    )
    try:
        hanging = [
            asyncio.create_task(service._send_with_pywebpush(_web_subscription(), "{}"))
            for _ in range(4)
        ]
        await asyncio.sleep(0.1)
        assert await asyncio.wait_for(store.pending_count(), timeout=2.0) == 0
    finally:
        released.set()
        await asyncio.gather(*hanging, return_exceptions=True)
        await service.stop()
        default.shutdown(wait=True)


def _web_subscription() -> WebPushSubscription:
    return WebPushSubscription(
        endpoint=ENDPOINT,
        p256dh=KEY,
        auth=KEY,
        session_jti="j" * 20,
        username="admin",
        expires_at=9_999_999_999,
    )


@pytest.mark.asyncio
async def test_an_expired_registration_frees_the_endpoint(tmp_path: Path) -> None:
    """GW-2: a row nobody owns any more must not lock the next owner of the same browser out."""
    store = PushStore(tmp_path / "push.sqlite3")
    from dataclasses import replace

    stale = replace(_web_subscription(), expires_at=time.time() - 1)
    assert await store.upsert_web(stale)
    assert await store.upsert_web(replace(_web_subscription(), username="mallory"))
    assert [item.username for item in await store.list_web("mallory")] == ["mallory"]
