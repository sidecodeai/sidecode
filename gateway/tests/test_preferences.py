"""Amendments A35 and A41: the account's preferences, and what a device does about a limit.

Four things are checked here — the store, the two REST paths, the fan-out to every app and device
of the account, and the three pushes a ``resume`` event cues — plus the frozen fixtures for each.
A41 added the Settings screen's own preferences to the object, each absent until it is set.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.apns import ApnsProvider, ApnsResponse
from rc_gateway.frames import FORWARDED_BY_SESSION
from rc_gateway.hub import SessionResumeNotice
from rc_gateway.preference_store import DEFAULTS, FIELDS, Preferences, PreferenceStore
from rc_gateway.push import (
    KIND_LIMIT_REACHED,
    KIND_RESUME_DROPPED,
    KIND_RESUMED,
    PushService,
    build_payload,
    resume_kind,
)
from rc_gateway.push_store import ApnsRegistration, PushStore, WebPushSubscription
from rc_gateway.routes.preference_routes import CHECKS

from .conftest import (
    FIXTURE_DIR,
    FakeWebPushSender,
    add_member,
    collect_until,
    device_hello,
    drain_until,
    enroll_device,
    no_unseen,
    session_summary,
)

SESSION_ID = "6d1f3c58-8b2e-4d67-9a4f-2e7c1b0d5a93"
ENDPOINT = "https://push.example.com/subscription/resume"
KEY = "A" * 32


def fixture(*parts: str) -> Any:
    return json.loads((FIXTURE_DIR.joinpath(*parts)).read_text(encoding="utf-8"))


def all_preferences() -> dict[str, Any]:
    """Every field the object can hold, as the frozen A41 fixtures carry them."""
    return dict(fixture("http", "preferences.response.json")["preferences"])


# ---- the store ----


async def test_an_account_with_no_row_reads_the_defaults(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "preferences.sqlite3")
    assert await store.get("nobody") == DEFAULTS
    assert DEFAULTS.resume_after_limit is False


async def test_a_switch_is_stored_per_account_and_survives_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "preferences.sqlite3"
    store = PreferenceStore(path)
    assert await store.patch("ada", {"resume_after_limit": True}) == Preferences(True)
    assert await store.get("grace") == DEFAULTS

    reopened = PreferenceStore(path)
    assert await reopened.get("ada") == Preferences(True)
    assert (await reopened.patch("ada", {})) == Preferences(True)
    assert await reopened.patch("ada", {"resume_after_limit": False}) == DEFAULTS


async def test_every_preference_round_trips_and_an_unset_one_is_absent(tmp_path: Path) -> None:
    path = tmp_path / "preferences.sqlite3"
    store = PreferenceStore(path)
    assert DEFAULTS.view() == {"resume_after_limit": False}

    stored = await store.patch("ada", all_preferences())
    assert stored.view() == all_preferences()
    assert await PreferenceStore(path).get("ada") == stored

    # An account that has set one preference reads that one and no more.
    grace = await store.patch("grace", {"timeline_detail": "detailed"})
    assert grace.view() == {"resume_after_limit": False, "timeline_detail": "detailed"}


async def test_a_preference_set_to_its_off_value_is_still_set(tmp_path: Path) -> None:
    """A41 hangs on the difference: absent is what makes an app write its own value up."""
    store = PreferenceStore(tmp_path / "preferences.sqlite3")
    assert (await store.get("ada")).polish_enabled is None
    set_off = await store.patch("ada", {"polish_enabled": False, "polish_model": ""})
    assert set_off.polish_enabled is False
    assert set_off.view() == {
        "resume_after_limit": False,
        "polish_enabled": False,
        "polish_model": "",
    }


async def test_a_database_from_before_the_settings_preferences_gains_the_columns(
    tmp_path: Path,
) -> None:
    """A column added to CREATE TABLE IF NOT EXISTS never reaches an existing DATA_DIR."""
    path = tmp_path / "preferences.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE preferences (
            username TEXT PRIMARY KEY,
            resume_after_limit INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL);
        INSERT INTO preferences VALUES ('ada', 1, 1788948000.0);
        """
    )
    legacy.commit()
    legacy.close()

    store = PreferenceStore(path)
    # The row written before A41 keeps the switch it had, with no Settings preference set.
    assert await store.get("ada") == Preferences(resume_after_limit=True)
    # And the columns the migration added are usable, on that row and after a reopen.
    written = await store.patch("ada", {"language": "zh-Hans"})
    assert written == Preferences(resume_after_limit=True, language="zh-Hans")
    assert await PreferenceStore(path).get("ada") == written


def test_the_settings_columns_are_added_once(tmp_path: Path) -> None:
    path = tmp_path / "preferences.sqlite3"
    PreferenceStore(path)
    PreferenceStore(path)
    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(preferences)")]
    assert columns.count("language") == 1
    assert set(FIELDS) <= set(columns)


async def test_a_deleted_account_leaves_no_switches(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "preferences.sqlite3")
    await store.patch("ada", {"resume_after_limit": True})
    await store.remove_for_user("ada")
    assert await store.get("ada") == DEFAULTS


def test_the_store_file_is_not_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "preferences.sqlite3"
    PreferenceStore(path)
    assert path.stat().st_mode & 0o077 == 0


# ---- REST ----


def test_preferences_start_off_and_match_the_fixture(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.get("/api/preferences", headers=auth)
    assert response.status_code == 200
    assert set(response.json()) == set(fixture("http", "preferences.response.json"))
    assert response.json() == {"preferences": {"resume_after_limit": False}}


def test_patch_sets_the_switch_and_returns_the_whole_object(
    client: TestClient, auth: dict[str, str]
) -> None:
    patch = fixture("http", "preferences.patch.request.json")
    response = client.patch("/api/preferences", json=patch, headers=auth)
    assert response.status_code == 200
    assert response.json() == {"preferences": {"resume_after_limit": True}}
    assert client.get("/api/preferences", headers=auth).json()["preferences"] == {
        "resume_after_limit": True
    }


def test_preferences_need_a_credential(client: TestClient) -> None:
    assert client.get("/api/preferences").status_code == 401
    assert client.patch("/api/preferences", json={"resume_after_limit": True}).status_code == 401


def test_one_account_never_reads_or_writes_another(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "ada")
    client.patch("/api/preferences", json={"resume_after_limit": True}, headers=member)
    assert client.get("/api/preferences", headers=member).json()["preferences"] == {
        "resume_after_limit": True
    }
    # The admin's own switch is untouched: there is no path here that names an account.
    assert client.get("/api/preferences", headers=auth).json()["preferences"] == {
        "resume_after_limit": False
    }


def test_the_settings_preferences_are_stored_and_returned(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.patch("/api/preferences", json=all_preferences(), headers=auth)
    assert response.status_code == 200
    assert response.json() == fixture("http", "preferences.response.json")
    assert client.get("/api/preferences", headers=auth).json()["preferences"] == all_preferences()


def test_a_patch_sets_only_the_fields_it_names(client: TestClient, auth: dict[str, str]) -> None:
    client.patch("/api/preferences", json={"language": "zh-Hans"}, headers=auth)
    # The empty model is a value, not an absence: it is how the person chooses no polish model.
    response = client.patch("/api/preferences", json={"polish_model": ""}, headers=auth)
    assert response.json()["preferences"] == {
        "resume_after_limit": False,
        "language": "zh-Hans",
        "polish_model": "",
    }


def test_the_longest_values_the_schema_allows_are_accepted(
    client: TestClient, auth: dict[str, str]
) -> None:
    body = {"stt_language": "x" * 32, "polish_model": "m" * 128}
    response = client.patch("/api/preferences", json=body, headers=auth)
    assert response.json()["preferences"] == {"resume_after_limit": False, **body}


def test_a_non_boolean_switch_is_a_bad_request(client: TestClient, auth: dict[str, str]) -> None:
    response = client.patch("/api/preferences", json={"resume_after_limit": "yes"}, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


@pytest.mark.parametrize(
    "body",
    [
        {"language": "fr"},
        {"language": "zh"},
        {"language": True},
        {"polish_strength": "gentle"},
        {"timeline_detail": "full"},
        {"timeline_detail": None},
        {"polish_enabled": "yes"},
        {"resume_after_limit": 1},
        {"stt_language": ""},
        {"stt_language": "x" * 33},
        {"stt_language": 3},
        {"polish_model": "m" * 129},
        {"polish_model": ["gpt-5.4-mini"]},
    ],
)
def test_a_value_the_schema_does_not_allow_is_refused_and_stores_nothing(
    client: TestClient, auth: dict[str, str], body: dict[str, Any]
) -> None:
    response = client.patch("/api/preferences", json=body, headers=auth)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert client.get("/api/preferences", headers=auth).json()["preferences"] == {
        "resume_after_limit": False
    }


def test_every_preference_on_the_wire_has_a_check() -> None:
    """A field added to `FIELDS` with no rule would be stored unread or fail at runtime."""
    assert set(CHECKS) == set(FIELDS)


def test_a_field_this_gateway_does_not_know_is_ignored(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.patch("/api/preferences", json={"invented_later": True}, headers=auth)
    assert response.status_code == 200
    assert response.json() == {"preferences": {"resume_after_limit": False}}


def test_a_deleted_account_takes_its_switches_with_it(
    client: TestClient, auth: dict[str, str]
) -> None:
    member = add_member(client, auth, "ada")
    client.patch("/api/preferences", json={"resume_after_limit": True}, headers=member)
    assert client.delete("/api/users/ada", headers=auth).status_code == 200
    recreated = add_member(client, auth, "ada")
    assert client.get("/api/preferences", headers=recreated).json()["preferences"] == {
        "resume_after_limit": False
    }


# ---- sockets ----


def test_hello_carries_the_account_preferences(client: TestClient, auth: dict[str, str]) -> None:
    client.patch("/api/preferences", json=all_preferences(), headers=auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        hello = drain_until(app, "hello")
    assert hello["preferences"] == all_preferences()


def test_hello_carries_only_the_preferences_that_have_been_set(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A41: `hello.json` is a partial object, and absent is how an app knows to write its own up."""
    partial = fixture("app", "hello.json")["preferences"]
    assert set(partial) < set(FIELDS)
    client.patch("/api/preferences", json=partial, headers=auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        hello = drain_until(app, "hello")
    assert hello["preferences"] == partial


def test_a_device_is_told_the_preferences_right_after_the_ack(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    client.patch("/api/preferences", json=all_preferences(), headers=auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello())
        ack = device.receive_json()
        frame = device.receive_json()
    assert ack["type"] == "hello_ack"
    assert frame == fixture("device", "preferences.json")


def test_a_change_reaches_every_app_and_device_of_the_account(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    other = add_member(client, auth, "ada")
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello())
        drain_until(device, "preferences")
        with (
            client.websocket_connect("/ws/app", headers=auth) as app,
            client.websocket_connect("/ws/app", headers=other) as stranger,
        ):
            drain_until(app, "hello")
            drain_until(stranger, "hello")
            client.patch("/api/preferences", json=all_preferences(), headers=auth)
            announced = drain_until(app, "preferences.updated")
            pushed = drain_until(device, "preferences")
            # A switch is one account's (A24). The PATCH returns only after its broadcast, so a
            # frame that leaked to the other account is already queued ahead of this marker.
            stranger.send_json(
                {"type": "session.subscribe", "id": "marker", "session_id": SESSION_ID}
            )
            quiet = collect_until(stranger, "reply")
            client.patch("/api/preferences", json={"resume_after_limit": True}, headers=other)
            theirs = drain_until(stranger, "preferences.updated")

    assert announced == fixture("app", "preferences.updated.json")
    assert pushed == fixture("device", "preferences.json")
    assert [frame["type"] for frame in quiet] == ["reply"]
    assert theirs["preferences"] == {"resume_after_limit": True}


def test_a_one_field_change_announces_the_whole_object(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A41: an app takes every `preferences.updated` as the truth, so all of it goes out."""
    client.patch("/api/preferences", json={"polish_model": "gpt-5.4-mini"}, headers=auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        client.patch("/api/preferences", json={"polish_strength": "strong"}, headers=auth)
        announced = drain_until(app, "preferences.updated")
    assert announced["preferences"] == {
        "resume_after_limit": False,
        "polish_model": "gpt-5.4-mini",
        "polish_strength": "strong",
    }


def test_the_last_write_to_arrive_is_the_one_everyone_reads(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A41: the gateway is the one writer, and the order of arrival is the order of truth."""
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        client.patch("/api/preferences", json={"timeline_detail": "detailed"}, headers=auth)
        client.patch("/api/preferences", json={"timeline_detail": "simple"}, headers=auth)
        announced = [
            drain_until(app, "preferences.updated")["preferences"]["timeline_detail"],
            drain_until(app, "preferences.updated")["preferences"]["timeline_detail"],
        ]
    assert announced == ["detailed", "simple"]
    stored = client.get("/api/preferences", headers=auth).json()["preferences"]
    assert stored["timeline_detail"] == "simple"


def test_a_patch_that_changes_nothing_announces_nothing(
    client: TestClient, auth: dict[str, str]
) -> None:
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        client.patch("/api/preferences", json={"resume_after_limit": False}, headers=auth)
        # A frame the gateway does send, behind the one it must not: `ping` would take 25 s.
        client.patch("/api/preferences", json={"resume_after_limit": True}, headers=auth)
        assert drain_until(app, "preferences.updated")["preferences"] == {
            "resume_after_limit": True
        }


# ---- forwarding ----


def test_the_two_resume_requests_are_forwarded() -> None:
    assert {"session.resume_set", "session.resume_cancel"} <= FORWARDED_BY_SESSION


def test_resume_set_reaches_the_device_and_its_reply_comes_back(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    request = fixture("app", "session.resume_set.json")
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        drain_until(device, "preferences")
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json(request)
            seen = drain_until(device, "session.resume_set")
            assert seen["at"] == request["at"]
            assert seen["device_id"] == enrolled["device_id"]
            reply = dict(fixture("app", "reply.session.resume_set.json"))
            reply["from"] = seen["from"]
            device.send_json(reply)
            delivered = drain_until(app, "reply")
    assert delivered["id"] == request["id"]
    assert delivered["result"]["session"]["resume"] == {
        "at": 1788967860000,
        "estimated": False,
        "attempts": 0,
        "window_minutes": 300,
    }


def test_a_pending_resume_survives_the_session_index(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    summary = dict(fixture("objects", "session.resume-pending.json"))
    summary["device_id"] = enrolled["device_id"]
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(sessions=[summary]))
        drain_until(device, "preferences")
        with client.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
    listed = client.get("/api/sessions", headers=auth).json()["sessions"]
    assert [item["resume"] for item in listed] == [summary["resume"]]
    assert [item["resume"] for item in hello["sessions"]] == [summary["resume"]]


def test_a_limit_stop_and_a_resume_event_travel_untouched(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    completed = fixture("events", "turn_completed.limit.json")
    scheduled = fixture("events", "resume.json")
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        drain_until(device, "preferences")
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            app.send_json({"type": "session.subscribe", "id": "sub-1", "session_id": SESSION_ID})
            drain_until(app, "reply")
            for event in (completed, scheduled):
                device.send_json(
                    {"type": "session.event", "session_id": SESSION_ID, "event": event}
                )
            first = drain_until(app, "session.event")
            second = drain_until(app, "session.event")
    assert first["event"] == completed
    assert first["event"]["limit"] == {"window_minutes": 300, "resets_at": 1788966000000}
    assert second["event"] == scheduled


# ---- push ----


def test_resume_kinds_follow_the_amendment() -> None:
    assert resume_kind("scheduled") == KIND_LIMIT_REACHED
    assert resume_kind("fired") == KIND_RESUMED
    assert resume_kind("dropped") == KIND_RESUME_DROPPED
    assert resume_kind("rescheduled") is None
    assert resume_kind("cancelled") is None
    assert resume_kind("") is None


def test_the_limit_push_payload_matches_the_fixture() -> None:
    expected = fixture("http", "push.payload.limit.json")
    payload = build_payload(
        expected["rc"]["kind"],
        {
            "device_id": expected["rc"]["device_id"],
            "session_id": expected["rc"]["session_id"],
        },
        expected["rc"]["device_name"],
        0,
    )
    # The fixture predates A47, after which every push also carries the account's count.
    assert payload == {"rc": {**expected["rc"], "badge": 0}}


def _subscribe(client: TestClient, auth: dict[str, str]) -> None:
    response = client.post(
        "/api/push/web/subscribe",
        json={"subscription": {"endpoint": ENDPOINT, "keys": {"p256dh": KEY, "auth": KEY}}},
        headers=auth,
    )
    assert response.status_code == 200, response.text


def _resume_event(seq: int, status: str) -> dict[str, Any]:
    event: dict[str, Any] = {
        "seq": seq,
        "ts": 1788948000000 + seq,
        "kind": "resume",
        "status": status,
    }
    if status in ("scheduled", "rescheduled"):
        event["at"] = 1788966060000
        event["estimated"] = False
    return event


def _settle(sender: FakeWebPushSender, pushes: int) -> None:
    deadline = time.monotonic() + 2.0
    while len(sender.sent) < pushes and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(sender.sent) == pushes, [payload["rc"]["kind"] for _, payload in sender.sent]


def test_resume_events_push_three_kinds_and_stay_silent_for_two(
    client: TestClient, auth: dict[str, str], web_sender: FakeWebPushSender
) -> None:
    enrolled = enroll_device(client, auth)
    _subscribe(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(
            device_hello(sessions=[session_summary(SESSION_ID, enrolled["device_id"])])
        )
        drain_until(device, "preferences")
        # An app socket that never subscribes: nobody is watching this session, so the
        # notification is the only way its owner would hear about the pause.
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            expected = {"scheduled": 1, "rescheduled": 1, "fired": 2, "cancelled": 2, "dropped": 3}
            for seq, status in enumerate(expected, start=1):
                device.send_json(
                    {
                        "type": "session.event",
                        "session_id": SESSION_ID,
                        "event": _resume_event(seq, status),
                    }
                )
                # One at a time, so a silent status is proved silent rather than raced past.
                _settle(web_sender, expected[status])
            # And nothing arrives late from the two that must say nothing at all.
            time.sleep(0.2)
            assert len(web_sender.sent) == 3

    kinds = [payload["rc"]["kind"] for _, payload in web_sender.sent]
    assert kinds == [KIND_LIMIT_REACHED, KIND_RESUMED, KIND_RESUME_DROPPED]
    endpoint, first = web_sender.sent[0]
    assert endpoint == ENDPOINT
    assert first["rc"]["session_id"] == SESSION_ID
    assert first["rc"]["device_id"] == enrolled["device_id"]
    assert first["rc"]["title"] == "mac-studio: paused by the usage limit"
    assert set(first) == {"rc"}
    assert web_sender.sent[1][1]["rc"]["title"] == "mac-studio: resumed after the limit reset"
    assert web_sender.sent[2][1]["rc"]["title"] == "mac-studio: not resumed"


async def test_a_resume_notice_reaches_web_push_and_apns(tmp_path: Path) -> None:
    """The hook itself, both transports, without a socket in the way."""
    store = PushStore(tmp_path / "push.sqlite3")
    await store.upsert_web(
        WebPushSubscription(
            endpoint=ENDPOINT,
            p256dh=KEY,
            auth=KEY,
            session_jti="j" * 20,
            username="admin",
            expires_at=9_999_999_999,
        )
    )
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
    apns_bodies: list[bytes] = []

    async def apns_sender(url: str, headers: dict[str, str], payload: bytes) -> ApnsResponse:
        apns_bodies.append(payload)
        return ApnsResponse(200)

    provider = ApnsProvider(
        "ABCDE12345",
        "FGHIJ67890",
        "com.example.app",
        _p256_pem(),
        environment="sandbox",
        sender=apns_sender,
    )
    web = FakeWebPushSender()
    service = PushService(
        store,
        device_name=_device_name,
        badge_count=no_unseen,
        # Web delivery is off without a VAPID pair, and this test wants both transports.
        vapid_private_key="key",
        vapid_contact="mailto:admin@example.com",
        web_sender=web,
        apns=provider,
    )

    for status in ("scheduled", "rescheduled", "fired", "cancelled", "dropped"):
        await service.on_session_resume(
            SessionResumeNotice(
                status=status,
                session_id=SESSION_ID,
                device_id="c5efb1ec-2912-4619-90f7-93b5172fd712",
                owner="admin",
                has_active_subscriber=False,
            )
        )

    assert [payload["rc"]["kind"] for _, payload in web.sent] == [
        KIND_LIMIT_REACHED,
        KIND_RESUMED,
        KIND_RESUME_DROPPED,
    ]
    assert [json.loads(body)["rc"]["kind"] for body in apns_bodies] == [
        KIND_LIMIT_REACHED,
        KIND_RESUMED,
        KIND_RESUME_DROPPED,
    ]
    assert json.loads(apns_bodies[0])["aps"]["alert"]["body"] == (
        "mac-studio-office: paused by the usage limit"
    )


async def test_no_resume_push_while_an_app_is_watching(tmp_path: Path) -> None:
    store = PushStore(tmp_path / "push.sqlite3")
    await store.upsert_web(
        WebPushSubscription(
            endpoint=ENDPOINT,
            p256dh=KEY,
            auth=KEY,
            session_jti="j" * 20,
            username="admin",
            expires_at=9_999_999_999,
        )
    )
    web = FakeWebPushSender()
    service = PushService(
        store,
        device_name=_device_name,
        badge_count=no_unseen,
        vapid_private_key="key",
        vapid_contact="mailto:admin@example.com",
        web_sender=web,
    )
    await service.on_session_resume(
        SessionResumeNotice(
            status="scheduled",
            session_id=SESSION_ID,
            device_id="d",
            owner="admin",
            has_active_subscriber=True,
        )
    )
    assert web.sent == []


async def _device_name(device_id: str) -> str:
    return "mac-studio-office"


def _p256_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
