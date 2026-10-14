"""A47: the count in every push, and the badge-only APNs push once a count settles.

Every push carries the account's unarchived `unseen` sessions as `rc.badge` and, to APNs, as
`aps.badge`. A change no push carried reaches each of the account's phones as one notification
with the badge and nothing else, three seconds after the count stopped moving, and never reaches a
browser. The quiet period runs on a virtual clock here, so nothing waits for real time.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.badge import BADGE_SETTLE_SECONDS, BadgeChange, BadgeSettler
from rc_gateway.connections import AppConnection, DeviceConnection
from rc_gateway.hub import Hub
from rc_gateway.push import (
    KIND_BADGE,
    KIND_ERROR,
    KIND_NEEDS_APPROVAL,
    KIND_TURN_COMPLETED,
    PushService,
)
from rc_gateway.push_store import ApnsRegistration, PushStore, WebPushSubscription
from rc_gateway.state import GatewayState

from .conftest import (
    ApnsRecorder,
    FakeWebPushSender,
    VirtualClock,
    apns_provider,
    device_hello,
    drain_until,
    enroll_device,
    fake_app,
    fake_device,
    make_config,
    session_summary,
    sign_in,
)

DEVICE_ID = "c5efb1ec-2912-4619-90f7-93b5172fd712"
SESSION_ID = "ad2c9abb-4a1e-470a-835c-228778fc17f0"
OTHER_ID = "ad2c9abb-4a1e-470a-835c-228778fc17f1"
TOKEN = "ab" * 32
ENDPOINT = "https://push.example.com/subscription/badge"
KEY = "A" * 32
CHANGE = BadgeChange(owner="admin", device_id=DEVICE_ID, session_id=SESSION_ID)


def _registration() -> ApnsRegistration:
    return ApnsRegistration(
        device_token=TOKEN,
        environment="sandbox",
        bundle_id="com.example.app",
        session_jti="j" * 20,
        username="admin",
        expires_at=9_999_999_999,
    )


def _subscription() -> WebPushSubscription:
    return WebPushSubscription(
        endpoint=ENDPOINT,
        p256dh=KEY,
        auth=KEY,
        session_jti="j" * 20,
        username="admin",
        expires_at=9_999_999_999,
    )


async def _device_name(device_id: str) -> str:
    return "mac-studio-office"


def _counting(counts: dict[str, int]) -> Callable[[str], Awaitable[int]]:
    async def count(username: str) -> int:
        return counts.get(username, 0)

    return count


async def _service(
    tmp_path: Path,
    recorder: ApnsRecorder,
    counts: dict[str, int],
    *,
    web: FakeWebPushSender | None = None,
) -> PushService:
    store = PushStore(tmp_path / "push.sqlite3")
    await store.upsert_apns(_registration())
    if web is not None:
        await store.upsert_web(_subscription())
    return PushService(
        store,
        device_name=_device_name,
        badge_count=_counting(counts),
        vapid_private_key="key" if web is not None else "",
        vapid_contact="mailto:admin@example.com" if web is not None else "",
        web_sender=web,
        apns=apns_provider(recorder),
    )


# ---- settling ----


async def test_one_change_goes_out_three_seconds_after_the_count_stops_moving() -> None:
    clock = VirtualClock()
    sent: list[BadgeChange] = []

    async def send(change: BadgeChange) -> None:
        sent.append(change)

    settler = BadgeSettler(send, sleep=clock.sleep)
    later = BadgeChange(owner="admin", device_id=DEVICE_ID, session_id=OTHER_ID)
    settler.changed(CHANGE)
    await clock.advance(BADGE_SETTLE_SECONDS - 0.1)
    assert sent == []
    settler.changed(later)
    await clock.advance(BADGE_SETTLE_SECONDS - 0.1)
    assert sent == []
    await clock.advance(0.1)
    assert sent == [later]
    await clock.advance(BADGE_SETTLE_SECONDS * 10)
    assert sent == [later]


async def test_each_account_settles_on_its_own() -> None:
    clock = VirtualClock()
    sent: list[BadgeChange] = []

    async def send(change: BadgeChange) -> None:
        sent.append(change)

    settler = BadgeSettler(send, sleep=clock.sleep)
    theirs = BadgeChange(owner="mallory", device_id=DEVICE_ID, session_id=OTHER_ID)
    settler.changed(CHANGE)
    await clock.advance(2.0)
    settler.changed(theirs)
    await clock.advance(1.0)
    assert sent == [CHANGE]
    await clock.advance(2.0)
    assert sent == [CHANGE, theirs]


async def test_a_change_during_a_send_waits_its_own_turn() -> None:
    """A delivery under way is never cancelled by the change that follows it."""
    clock = VirtualClock()
    release = asyncio.Event()
    sent: list[BadgeChange] = []

    async def send(change: BadgeChange) -> None:
        await release.wait()
        sent.append(change)

    settler = BadgeSettler(send, sleep=clock.sleep)
    later = BadgeChange(owner="admin", device_id=DEVICE_ID, session_id=OTHER_ID)
    settler.changed(CHANGE)
    await clock.advance(BADGE_SETTLE_SECONDS)
    settler.changed(later)
    release.set()
    await clock.advance(0)
    assert sent == [CHANGE]
    await clock.advance(BADGE_SETTLE_SECONDS)
    assert sent == [CHANGE, later]


async def test_a_count_still_settling_is_sent_when_the_gateway_stops() -> None:
    clock = VirtualClock()
    sent: list[BadgeChange] = []

    async def send(change: BadgeChange) -> None:
        sent.append(change)

    settler = BadgeSettler(send, sleep=clock.sleep)
    settler.changed(CHANGE)
    await settler.stop()
    assert sent == [CHANGE]
    await clock.advance(BADGE_SETTLE_SECONDS)
    assert sent == [CHANGE]


# ---- the count in every push ----


async def test_every_push_carries_the_count(tmp_path: Path) -> None:
    recorder = ApnsRecorder()
    web = FakeWebPushSender()
    service = await _service(tmp_path, recorder, {"admin": 2}, web=web)
    await service.notify(
        KIND_NEEDS_APPROVAL,
        {"device_id": DEVICE_ID, "session_id": SESSION_ID},
        "mac-studio-office",
        "admin",
    )
    assert [payload["rc"]["badge"] for _, payload in web.sent] == [2]
    headers, body = recorder.requests[0]
    assert body["aps"]["badge"] == 2
    assert body["aps"]["alert"]["body"] == "mac-studio-office: approval needed"
    assert body["rc"]["badge"] == 2
    assert headers["apns-priority"] == "10"
    assert headers["apns-push-type"] == "alert"
    await service.stop()


async def test_the_badge_only_push_carries_the_count_and_nothing_else(tmp_path: Path) -> None:
    recorder = ApnsRecorder()
    web = FakeWebPushSender()
    service = await _service(tmp_path, recorder, {"admin": 2}, web=web)
    await service.push_badge(CHANGE)
    headers, body = recorder.requests[0]
    assert body == {
        "aps": {"badge": 2},
        "rc": {
            "v": 1,
            "kind": KIND_BADGE,
            "device_id": DEVICE_ID,
            "session_id": SESSION_ID,
            "device_name": "mac-studio-office",
            "title": "",
            "badge": 2,
        },
    }
    assert headers["apns-push-type"] == "alert"
    assert headers["apns-priority"] == "5"
    # A browser shows every push it receives, so none goes to Web Push.
    assert web.sent == []
    await service.stop()


async def test_a_count_the_last_push_carried_is_not_sent_again(tmp_path: Path) -> None:
    recorder = ApnsRecorder()
    counts = {"admin": 1}
    service = await _service(tmp_path, recorder, counts)
    await service.notify(
        KIND_TURN_COMPLETED, {"device_id": DEVICE_ID, "session_id": SESSION_ID}, "mac", "admin"
    )
    await service.push_badge(CHANGE)
    assert len(recorder.requests) == 1
    counts["admin"] = 0
    await service.push_badge(CHANGE)
    assert [body["aps"]["badge"] for body in recorder.bodies] == [1, 0]
    await service.push_badge(CHANGE)
    assert len(recorder.requests) == 2
    await service.stop()


async def test_no_badge_is_settled_without_apns(tmp_path: Path) -> None:
    clock = VirtualClock()
    asked: list[str] = []

    async def count(username: str) -> int:
        asked.append(username)
        return 1

    store = PushStore(tmp_path / "push.sqlite3")
    service = PushService(store, device_name=_device_name, badge_count=count, sleep=clock.sleep)
    service.on_badge_change(CHANGE)
    await clock.advance(BADGE_SETTLE_SECONDS)
    await service.stop()
    assert asked == []


# ---- the delivery journal ----


async def test_a_newer_push_replaces_a_badge_still_waiting(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.sqlite3")
    await store.enqueue(TOKEN, "sandbox", "badge-1", badge_only=True)
    await store.enqueue(TOKEN, "sandbox", "badge-2", badge_only=True)
    await store.enqueue("cd" * 32, "sandbox", "theirs", badge_only=True)
    await store.enqueue(TOKEN, "sandbox", "alert-1")
    await store.enqueue(TOKEN, "sandbox", "alert-2")
    await store.enqueue(TOKEN, "sandbox", "badge-3", badge_only=True)
    pending = sorted(item.payload for item in await store.claim_due())
    # Each delivery outdates the badge still waiting for the same phone; no alert is ever dropped.
    assert pending == ["alert-1", "alert-2", "badge-3", "theirs"]


async def test_a_badge_retried_later_keeps_its_priority(tmp_path: Path) -> None:
    recorder = ApnsRecorder(statuses=[503, 200])
    service = await _service(tmp_path, recorder, {"admin": 1})
    await service.push_badge(CHANGE)
    assert await service.store.pending_count() == 1
    assert await service.store.claim_due() == []
    await service.store.retry_at(1, 0.0)
    await service.flush_apns()
    assert [headers["apns-priority"] for headers, _ in recorder.requests] == ["5", "5"]
    assert await service.store.pending_count() == 0
    await service.stop()


async def test_a_journal_from_before_the_amendment_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "push.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE apns_deliveries (
            delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, device_token TEXT NOT NULL,
            environment TEXT NOT NULL, payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL, created_at REAL NOT NULL);
        INSERT INTO apns_deliveries(device_token, environment, payload, due_at, created_at)
            VALUES ('ab', 'sandbox', 'queued-before', 0, 0);
        """
    )
    legacy.commit()
    legacy.close()
    store = PushStore(path)
    await store.enqueue("ab", "sandbox", "badge", badge_only=True)
    claimed = {item.payload: item.badge_only for item in await store.claim_due()}
    assert claimed == {"queued-before": False, "badge": True}


# ---- the whole path, on virtual time ----


@dataclass
class Rig:
    state: GatewayState
    recorder: ApnsRecorder
    clock: VirtualClock
    device: DeviceConnection
    app: AppConnection
    device_id: str


async def _rig(tmp_path: Path) -> Rig:
    """A gateway with APNs, one device, one app socket and one registered phone, on a clock."""
    recorder = ApnsRecorder()
    clock = VirtualClock()
    provider = apns_provider(recorder)
    state = build_state(make_config(tmp_path), apns=provider)
    state.push = PushService(
        state.push_store,
        device_name=state.device_name,
        badge_count=state.badge_count,
        apns=provider,
        sleep=clock.sleep,
    )
    state.hub = Hub(
        state.index,
        state.devices,
        on_session_transition=state.push.on_session_transition,
        on_badge_change=state.push.on_badge_change,
    )
    grant = await state.devices.create_pairing("admin")
    enrolled = await state.devices.redeem(
        grant.code,
        name="mac-studio",
        platform="macos",
        hostname="studio.local",
        arch="arm64",
        client_version="0.1.0",
    )
    assert not isinstance(enrolled, str), enrolled
    await state.push_store.upsert_apns(_registration())
    device = fake_device(enrolled.device_id)
    app = fake_app()
    await state.hub.attach_device(device)
    await state.hub.attach_app(app)
    return Rig(state, recorder, clock, device, app, enrolled.device_id)


async def _publish(rig: Rig, session_id: str = SESSION_ID, **fields: Any) -> None:
    summary = session_summary(session_id, rig.device_id, **fields)
    await rig.state.hub.handle_device_frame(
        rig.device, {"type": "session.updated", "session": summary}
    )


async def _until(predicate: Callable[[], bool]) -> None:
    """Wait for work the hub ran off its read loop, which the virtual clock does not drive."""
    deadline = time.monotonic() + 2.0
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


def _seen(session_id: str, identifier: str) -> dict[str, Any]:
    return {"type": "session.seen", "id": identifier, "session_id": session_id}


async def _stop(rig: Rig) -> None:
    await rig.state.hub.stop()
    await rig.state.push.stop()


async def test_a_carried_count_costs_no_second_push(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    await _publish(rig, state="running")
    await _publish(rig, state="idle")
    await _until(lambda: len(rig.recorder.requests) == 1)
    assert rig.recorder.bodies[0]["rc"]["kind"] == KIND_TURN_COMPLETED
    assert rig.recorder.bodies[0]["aps"]["badge"] == 1
    await rig.clock.advance(BADGE_SETTLE_SECONDS)
    await asyncio.sleep(0.05)
    assert len(rig.recorder.requests) == 1
    await _stop(rig)


async def test_a_count_no_push_carried_reaches_the_phone_once_it_settles(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    await _publish(rig, SESSION_ID, state="running")
    await _publish(rig, OTHER_ID, state="running")
    await _publish(rig, SESSION_ID, state="idle")
    await _until(lambda: len(rig.recorder.requests) == 1)
    assert rig.recorder.bodies[0]["aps"]["badge"] == 1
    await rig.state.hub.handle_app_frame(
        rig.app, {"type": "session.subscribe", "id": "s1", "session_id": OTHER_ID}
    )

    # A turn that ends on screen is not pushed and is opened at once: the count is back where the
    # last push left it, so the phone hears nothing.
    await _publish(rig, OTHER_ID, state="idle")
    await rig.state.hub.handle_app_frame(rig.app, _seen(OTHER_ID, "s2"))
    await rig.clock.advance(BADGE_SETTLE_SECONDS)
    await asyncio.sleep(0.05)
    assert len(rig.recorder.requests) == 1

    # Marked while watched and left unopened: the count moved and no push carried it.
    await _publish(rig, OTHER_ID, state="running")
    await _publish(rig, OTHER_ID, state="idle")
    await rig.clock.advance(BADGE_SETTLE_SECONDS - 0.1)
    await asyncio.sleep(0.05)
    assert len(rig.recorder.requests) == 1
    await rig.clock.advance(0.1)
    await _until(lambda: len(rig.recorder.requests) == 2)
    headers, body = rig.recorder.requests[1]
    assert body["aps"] == {"badge": 2}
    assert body["rc"]["kind"] == KIND_BADGE
    assert body["rc"]["title"] == ""
    assert body["rc"]["session_id"] == OTHER_ID
    assert body["rc"]["device_name"] == "mac-studio"
    assert headers["apns-priority"] == "5"

    # Both opened elsewhere: one push, with the count at zero.
    await rig.state.hub.handle_app_frame(rig.app, _seen(SESSION_ID, "s3"))
    await rig.clock.advance(1.0)
    await rig.state.hub.handle_app_frame(rig.app, _seen(OTHER_ID, "s4"))
    await rig.clock.advance(BADGE_SETTLE_SECONDS)
    await _until(lambda: len(rig.recorder.requests) == 3)
    assert rig.recorder.bodies[2]["aps"] == {"badge": 0}
    await asyncio.sleep(0.05)
    assert len(rig.recorder.requests) == 3
    await _stop(rig)


def test_a_count_is_sent_at_the_latest_when_the_gateway_stops(tmp_path: Path) -> None:
    """Over the real sockets: the count of a mark cleared on the web reaches the phone."""
    recorder = ApnsRecorder()
    state = build_state(make_config(tmp_path), apns=apns_provider(recorder))
    with TestClient(create_app(state)) as client:
        auth = {"Authorization": f"Bearer {sign_in(client, 'admin', state.config.password)}"}
        registered = client.post(
            "/api/push/apns/register",
            json={"token": TOKEN, "environment": "sandbox", "bundle_id": "com.example.app"},
            headers=auth,
        )
        assert registered.status_code == 200, registered.text
        enrolled = enroll_device(client, auth)
        headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
        with client.websocket_connect("/ws/device", headers=headers) as device:
            running = session_summary(SESSION_ID, enrolled["device_id"], state="running")
            device.send_json(device_hello(sessions=[running]))
            device.receive_json()
            with client.websocket_connect("/ws/app", headers=auth) as app:
                drain_until(app, "hello")
                idle = session_summary(SESSION_ID, enrolled["device_id"], state="idle")
                device.send_json({"type": "session.updated", "session": idle})
                drain_until(app, "session.updated")
                # The alert counts the mark before anyone opens the session.
                _wait_until(lambda: len(recorder.requests) == 1)
                app.send_json({"type": "session.seen", "id": "s1", "session_id": SESSION_ID})
                drain_until(app, "reply")
    kinds = [(body["rc"]["kind"], body["aps"]["badge"]) for body in recorder.bodies]
    assert kinds == [(KIND_TURN_COMPLETED, 1), (KIND_BADGE, 0)]


def test_a_signed_out_phone_hears_no_more_counts(tmp_path: Path) -> None:
    """Signing out takes the phone's registration with it, so neither push reaches it."""
    recorder = ApnsRecorder()
    state = build_state(make_config(tmp_path), apns=apns_provider(recorder))
    with TestClient(create_app(state)) as client:
        phone = {"Authorization": f"Bearer {sign_in(client, 'admin', state.config.password)}"}
        registered = client.post(
            "/api/push/apns/register",
            json={"token": TOKEN, "environment": "sandbox", "bundle_id": "com.example.app"},
            headers=phone,
        )
        assert registered.status_code == 200, registered.text
        assert client.post("/api/logout", headers=phone).status_code == 200

        auth = {"Authorization": f"Bearer {sign_in(client, 'admin', state.config.password)}"}
        enrolled = enroll_device(client, auth)
        headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
        with client.websocket_connect("/ws/device", headers=headers) as device:
            running = session_summary(SESSION_ID, enrolled["device_id"], state="running")
            device.send_json(device_hello(sessions=[running]))
            device.receive_json()
            with client.websocket_connect("/ws/app", headers=auth) as app:
                drain_until(app, "hello")
                idle = session_summary(SESSION_ID, enrolled["device_id"], state="idle")
                device.send_json({"type": "session.updated", "session": idle})
                drain_until(app, "session.updated")
                app.send_json({"type": "session.seen", "id": "s1", "session_id": SESSION_ID})
                drain_until(app, "reply")
    assert recorder.requests == []


def test_pushes_count_the_accounts_marked_sessions(
    client: TestClient, auth: dict[str, str], web_sender: FakeWebPushSender
) -> None:
    """Counted after the push's own change, unarchived only, and fallen when one is opened."""
    enrolled = enroll_device(client, auth)
    subscribed = client.post(
        "/api/push/web/subscribe",
        json={"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": KEY, "auth": KEY}}},
        headers=auth,
    )
    assert subscribed.status_code == 200, subscribed.text
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    device_id = enrolled["device_id"]

    def publish(device: Any, app: Any, session_id: str, **fields: Any) -> None:
        summary = session_summary(session_id, device_id, **fields)
        device.send_json({"type": "session.updated", "session": summary})
        drain_until(app, "session.updated")

    with client.websocket_connect("/ws/device", headers=headers) as device:
        sessions = [
            session_summary(item, device_id, state="running") for item in (SESSION_ID, OTHER_ID)
        ]
        device.send_json(device_hello(sessions=sessions))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            publish(device, app, SESSION_ID, state="idle")
            _wait_for(web_sender, 1)
            publish(device, app, OTHER_ID, state="needs_approval")
            _wait_for(web_sender, 2)
            app.send_json({"type": "session.seen", "id": "s1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            publish(device, app, OTHER_ID, state="idle")
            _wait_for(web_sender, 3)
            publish(device, app, OTHER_ID, state="idle", archived=True)
            publish(device, app, OTHER_ID, state="error", archived=True)
            _wait_for(web_sender, 4)
    counts = [(payload["rc"]["kind"], payload["rc"]["badge"]) for _, payload in web_sender.sent]
    assert counts == [
        (KIND_TURN_COMPLETED, 1),
        (KIND_NEEDS_APPROVAL, 2),
        (KIND_TURN_COMPLETED, 1),
        (KIND_ERROR, 0),
    ]


def _wait_for(sender: FakeWebPushSender, pushes: int) -> None:
    _wait_until(lambda: len(sender.sent) >= pushes)
    assert len(sender.sent) == pushes, [payload["rc"]["kind"] for _, payload in sender.sent]


def _wait_until(predicate: Callable[[], bool]) -> None:
    """Wait on the test's thread for what the gateway's own thread delivers."""
    deadline = time.monotonic() + 2.0
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)
