"""Which watching app silences which push.

An app that is subscribed to a session and was used in the last minute silences Web Push, and
silences the alert for the phones unless it is one of the phones. The iPhone app stays subscribed
through the background and a locked screen, so its own socket says nothing about whether anyone is
looking; a browser or a desktop app that is being used does. The hub reports the watching sign-ins
(`jti`s) and push compares them with the account's APNs registrations.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.connections import AppConnection
from rc_gateway.hub import ACTIVE_APP_WINDOW_SECONDS, SessionResumeNotice, SessionTransition
from rc_gateway.push import KIND_LIMIT_REACHED, KIND_NEEDS_APPROVAL, KIND_NEEDS_INPUT, PushService
from rc_gateway.push_store import ApnsRegistration, PushStore, WebPushSubscription

from .conftest import (
    ApnsRecorder,
    FakeWebPushSender,
    HubRig,
    apns_provider,
    device_hello,
    drain_until,
    enroll_device,
    fake_app,
    hub_rig,
    make_config,
    no_unseen,
    session_summary,
    sign_in,
)

SESSION_ID = "99999999-8888-7777-6666-555555555555"
OTHER_SESSION_ID = "99999999-8888-7777-6666-555555555556"
PHONE_TOKEN = "ab" * 32
SECOND_PHONE_TOKEN = "cd" * 32
PHONE_JTI = "phone-sign-in"
SECOND_PHONE_JTI = "second-phone-sign-in"
BROWSER_JTI = "browser-sign-in"
ENDPOINT = "https://push.example.com/subscription/presence"
KEY = "A" * 32
FOREVER = 9_999_999_999.0


@dataclass
class Pushing:
    """A push service on a real store: the phones registered, one browser subscribed."""

    service: PushService
    apns: ApnsRecorder
    web: FakeWebPushSender

    @property
    def phone_kinds(self) -> list[str]:
        return [body["rc"]["kind"] for body in self.apns.bodies]

    @property
    def browser_kinds(self) -> list[str]:
        return [payload["rc"]["kind"] for _, payload in self.web.sent]


def _phone(token: str, jti: str, *, expires_at: float = FOREVER) -> ApnsRegistration:
    return ApnsRegistration(
        device_token=token,
        environment="sandbox",
        bundle_id="com.example.app",
        session_jti=jti,
        username="admin",
        expires_at=expires_at,
    )


async def _device_name(device_id: str) -> str:
    return "mac-studio"


async def _pushing(tmp_path: Path, *phones: ApnsRegistration) -> Pushing:
    store = PushStore(tmp_path / "push.sqlite3")
    for phone in phones:
        await store.upsert_apns(phone)
    await store.upsert_web(
        WebPushSubscription(
            endpoint=ENDPOINT,
            p256dh=KEY,
            auth=KEY,
            session_jti="web-push-sign-in",
            username="admin",
            expires_at=FOREVER,
        )
    )
    apns, web = ApnsRecorder(), FakeWebPushSender()
    service = PushService(
        store,
        device_name=_device_name,
        badge_count=no_unseen,
        vapid_private_key="key",
        vapid_contact="mailto:admin@example.com",
        web_sender=web,
        apns=apns_provider(apns),
    )
    return Pushing(service, apns, web)


async def _gateway(tmp_path: Path, pushing: Pushing) -> HubRig:
    """A hub that pushes through the service, with the session already running."""
    rig = await hub_rig(
        tmp_path,
        on_session_transition=pushing.service.on_session_transition,
        on_session_resume=pushing.service.on_session_resume,
    )
    await _publish(rig, "running")
    return rig


async def _publish(rig: HubRig, state: str) -> None:
    summary = session_summary(SESSION_ID, rig.device_id, state=state)
    await rig.hub.handle_device_frame(rig.device, {"type": "session.updated", "session": summary})


async def _watch(rig: HubRig, jti: str) -> AppConnection:
    """An app socket signed in as `jti` that has just subscribed to the session."""
    app = fake_app("admin", jti)
    await rig.hub.attach_app(app)
    subscribe = {"type": "session.subscribe", "id": f"watch-{jti}", "session_id": SESSION_ID}
    await rig.hub.handle_app_frame(app, subscribe)
    return app


async def _finish(rig: HubRig, pushing: Pushing | None = None) -> None:
    """Let every notification the hub spawned run out, then stop what they were talking to."""
    await rig.hub.stop()
    if pushing is not None:
        await pushing.service.stop()
    await rig.index.close()


# ---- what the hub reports ----


async def test_a_transition_and_a_resume_name_the_sign_ins_watching_the_session(
    tmp_path: Path,
) -> None:
    transitions: list[SessionTransition] = []
    notices: list[SessionResumeNotice] = []

    async def on_transition(transition: SessionTransition) -> None:
        transitions.append(transition)

    async def on_resume(notice: SessionResumeNotice) -> None:
        notices.append(notice)

    rig = await hub_rig(tmp_path, on_session_transition=on_transition, on_session_resume=on_resume)
    await _publish(rig, "running")
    await _watch(rig, PHONE_JTI)
    await _watch(rig, BROWSER_JTI)
    # Subscribed, but nobody has used it for longer than the window.
    stale = await _watch(rig, "stale-sign-in")
    stale.last_active_at = time.monotonic() - ACTIVE_APP_WINDOW_SECONDS - 1
    # Used, but not subscribed to this session.
    await rig.hub.attach_app(fake_app("admin", "elsewhere-sign-in"))
    # Another account's socket is never reported, however it came to be subscribed.
    stranger = fake_app("mallory", "stranger-sign-in")
    await rig.hub.attach_app(stranger)
    stranger.subscribe(SESSION_ID, 0)

    await _publish(rig, "needs_approval")
    event = {"seq": 1, "kind": "resume", "status": "scheduled", "at": 1788966060000}
    await rig.hub.handle_device_frame(
        rig.device, {"type": "session.event", "session_id": SESSION_ID, "event": event}
    )
    await _finish(rig)

    watching = frozenset({PHONE_JTI, BROWSER_JTI})
    assert [item.active_subscribers for item in transitions] == [watching]
    assert [item.active_subscribers for item in notices] == [watching]


# ---- what push does with it ----


async def test_nobody_watching_alerts_the_phone_and_the_browser(tmp_path: Path) -> None:
    pushing = await _pushing(tmp_path, _phone(PHONE_TOKEN, PHONE_JTI))
    rig = await _gateway(tmp_path, pushing)
    await _publish(rig, "needs_approval")
    await _finish(rig, pushing)
    assert pushing.phone_kinds == [KIND_NEEDS_APPROVAL]
    assert pushing.browser_kinds == [KIND_NEEDS_APPROVAL]


async def test_a_phone_watching_alone_still_gets_the_alert(tmp_path: Path) -> None:
    pushing = await _pushing(tmp_path, _phone(PHONE_TOKEN, PHONE_JTI))
    rig = await _gateway(tmp_path, pushing)
    await _watch(rig, PHONE_JTI)
    await _publish(rig, "needs_approval")
    await _finish(rig, pushing)
    assert pushing.phone_kinds == [KIND_NEEDS_APPROVAL]
    # Web Push keeps the old rule: any watcher silences it, the phone's included.
    assert pushing.browser_kinds == []


async def test_every_registered_phone_watching_still_gets_the_alert(tmp_path: Path) -> None:
    pushing = await _pushing(
        tmp_path, _phone(PHONE_TOKEN, PHONE_JTI), _phone(SECOND_PHONE_TOKEN, SECOND_PHONE_JTI)
    )
    rig = await _gateway(tmp_path, pushing)
    await _watch(rig, PHONE_JTI)
    await _watch(rig, SECOND_PHONE_JTI)
    await _publish(rig, "needs_approval")
    await _finish(rig, pushing)
    assert pushing.phone_kinds == [KIND_NEEDS_APPROVAL, KIND_NEEDS_APPROVAL]


async def test_a_watching_app_that_is_not_a_phone_silences_the_alert(tmp_path: Path) -> None:
    pushing = await _pushing(tmp_path, _phone(PHONE_TOKEN, PHONE_JTI))
    rig = await _gateway(tmp_path, pushing)
    await _watch(rig, BROWSER_JTI)
    await _publish(rig, "needs_approval")
    await _finish(rig, pushing)
    assert pushing.phone_kinds == []
    assert pushing.browser_kinds == []


async def test_a_browser_watching_beside_the_phone_silences_the_alert(tmp_path: Path) -> None:
    pushing = await _pushing(tmp_path, _phone(PHONE_TOKEN, PHONE_JTI))
    rig = await _gateway(tmp_path, pushing)
    await _watch(rig, PHONE_JTI)
    await _watch(rig, BROWSER_JTI)
    await _publish(rig, "needs_approval")
    await _finish(rig, pushing)
    assert pushing.phone_kinds == []
    assert pushing.browser_kinds == []


@pytest.mark.parametrize(
    ("expires_at", "alerts"), [(FOREVER, 2), (1.0, 0)], ids=["registered", "expired"]
)
async def test_an_expired_registration_does_not_make_its_app_a_phone(
    tmp_path: Path, expires_at: float, alerts: int
) -> None:
    """A phone whose registration lapsed while its socket stayed open is a plain watcher."""
    pushing = await _pushing(
        tmp_path,
        _phone(PHONE_TOKEN, PHONE_JTI),
        _phone(SECOND_PHONE_TOKEN, SECOND_PHONE_JTI, expires_at=expires_at),
    )
    rig = await _gateway(tmp_path, pushing)
    await _watch(rig, SECOND_PHONE_JTI)
    await _publish(rig, "needs_approval")
    await _finish(rig, pushing)
    assert len(pushing.phone_kinds) == alerts


async def test_a_resume_notice_follows_the_same_rule(tmp_path: Path) -> None:
    pushing = await _pushing(tmp_path, _phone(PHONE_TOKEN, PHONE_JTI))

    def notice(*watching: str) -> SessionResumeNotice:
        return SessionResumeNotice(
            status="scheduled",
            session_id=SESSION_ID,
            device_id="d",
            owner="admin",
            active_subscribers=frozenset(watching),
        )

    await pushing.service.on_session_resume(notice(BROWSER_JTI, PHONE_JTI))
    assert pushing.phone_kinds == []
    assert pushing.browser_kinds == []

    await pushing.service.on_session_resume(notice(PHONE_JTI))
    assert pushing.phone_kinds == [KIND_LIMIT_REACHED]
    assert pushing.browser_kinds == []
    await pushing.service.stop()


# ---- over the real sockets ----


def _wait_until(predicate: Callable[[], bool]) -> None:
    """Wait on the test's thread for what the gateway's own thread delivers."""
    deadline = time.monotonic() + 2.0
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _sign_in_as_admin(client: TestClient, password: str) -> dict[str, str]:
    """Every sign-in is its own session, so its own `jti`."""
    return {"Authorization": f"Bearer {sign_in(client, 'admin', password)}"}


def _register_phone(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post(
        "/api/push/apns/register",
        json={"token": PHONE_TOKEN, "environment": "sandbox", "bundle_id": "com.example.app"},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _update(session_id: str, device_id: str, state: str) -> dict[str, Any]:
    return {
        "type": "session.updated",
        "session": session_summary(session_id, device_id, state=state),
    }


def test_the_sign_in_a_phone_registered_with_is_the_one_its_socket_carries(tmp_path: Path) -> None:
    apns = ApnsRecorder()
    state = build_state(make_config(tmp_path), apns=apns_provider(apns))
    with TestClient(create_app(state)) as client:
        phone = _sign_in_as_admin(client, state.config.password)
        _register_phone(client, phone)
        enrolled = enroll_device(client, phone)
        device_headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
        with client.websocket_connect("/ws/device", headers=device_headers) as device:
            running = session_summary(SESSION_ID, enrolled["device_id"], state="running")
            device.send_json(device_hello(sessions=[running]))
            device.receive_json()
            with client.websocket_connect("/ws/app", headers=phone) as app:
                drain_until(app, "hello")
                app.send_json({"type": "session.subscribe", "id": "p1", "session_id": SESSION_ID})
                drain_until(app, "reply")
                device.send_json(_update(SESSION_ID, enrolled["device_id"], "needs_approval"))
                drain_until(app, "session.updated")
                _wait_until(lambda: len(apns.requests) == 1)
    assert [body["rc"]["kind"] for body in apns.bodies] == [KIND_NEEDS_APPROVAL]


def test_a_browser_signed_in_beside_the_phone_silences_only_what_it_watches(
    tmp_path: Path,
) -> None:
    apns = ApnsRecorder()
    state = build_state(make_config(tmp_path), apns=apns_provider(apns))
    with TestClient(create_app(state)) as client:
        _register_phone(client, _sign_in_as_admin(client, state.config.password))
        browser = _sign_in_as_admin(client, state.config.password)
        enrolled = enroll_device(client, browser)
        device_headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
        with client.websocket_connect("/ws/device", headers=device_headers) as device:
            sessions = [
                session_summary(item, enrolled["device_id"], state="running")
                for item in (SESSION_ID, OTHER_SESSION_ID)
            ]
            device.send_json(device_hello(sessions=sessions))
            device.receive_json()
            with client.websocket_connect("/ws/app", headers=browser) as app:
                drain_until(app, "hello")
                app.send_json({"type": "session.subscribe", "id": "b1", "session_id": SESSION_ID})
                drain_until(app, "reply")
                device.send_json(_update(SESSION_ID, enrolled["device_id"], "needs_approval"))
                drain_until(app, "session.updated")
                device.send_json(_update(OTHER_SESSION_ID, enrolled["device_id"], "needs_input"))
                drain_until(app, "session.updated")
                _wait_until(lambda: len(apns.requests) == 1)
    # Read once the gateway has let every notification finish: the watched session sent nothing,
    # so the one alert is the other session's.
    assert [(body["rc"]["kind"], body["rc"]["session_id"]) for body in apns.bodies] == [
        (KIND_NEEDS_INPUT, OTHER_SESSION_ID)
    ]
