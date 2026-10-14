"""Amendment A13: one keepalive clock, and a grace period before a device is called offline.

The gateway's own 25 s / 90 s pings are the contract (§2.5). A second, shorter clock inside the
WebSocket server was closing devices whose event loop stalled, and the close was reported as
`online: false` the same instant, so devices flapped. These tests pin both halves plus the
reporting of refused upgrades that made the flapping invisible in the log.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.__main__ import server_config
from rc_gateway.frames import (
    CLOSE_PROTOCOL_ERROR,
    CLOSE_UNAUTHORIZED,
    PING_INTERVAL_SECONDS,
    SILENT_TIMEOUT_SECONDS,
    WS_MAX_MESSAGE_BYTES,
)
from rc_gateway.rejects import RejectionLog
from rc_gateway.state import GatewayState

from .conftest import (
    device_hello,
    drain_until,
    enroll_device,
    fake_device,
    frames_of,
    hub_rig,
    make_config,
    session_summary,
)

SESSION_ID = "cccccccc-dddd-eeee-ffff-000000000000"
GRACE = 0.2


# ---- one keepalive clock ----


def test_the_server_runs_no_keepalive_clock_of_its_own(tmp_path: Path) -> None:
    server = server_config(make_config(tmp_path))
    assert server.ws_ping_interval is None
    assert server.ws_ping_timeout is None
    # The contract's own clock is unchanged, and is now the only one on the link.
    assert PING_INTERVAL_SECONDS == 25.0
    assert SILENT_TIMEOUT_SECONDS == 90.0
    assert server.ws_max_size == WS_MAX_MESSAGE_BYTES


# ---- the offline grace period ----


@pytest.mark.asyncio
async def test_a_dropped_link_stays_online_until_the_grace_period_ends(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=GRACE)

    await rig.hub.detach_device(rig.device)
    assert rig.hub.device_online(rig.device_id) is True
    assert frames_of(rig.app) == []

    # The broadcast, not the flag: the period releases parked requests before it tells the apps,
    # so waiting on `device_online` alone would race the frame this asserts on.
    await _settle(lambda: not rig.app.queue.empty())
    assert rig.hub.device_online(rig.device_id) is False
    assert [frame["device"]["online"] for frame in frames_of(rig.app)] == [False]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_reconnect_inside_the_grace_period_says_nothing(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=GRACE)

    await rig.hub.detach_device(rig.device)
    replacement = fake_device(rig.device_id)
    await rig.hub.attach_device(replacement)

    # Past when the period would have ended, so a late broadcast would have landed by now.
    time.sleep(GRACE * 2)
    assert rig.hub.device_online(rig.device_id) is True
    assert frames_of(rig.app) == []
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_unauthorized_close_reports_offline_at_once(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=GRACE)

    # What `disconnect_device` does to a revoked device: 4401, which A13 excludes from the grace.
    await rig.device.stop(code=CLOSE_UNAUTHORIZED, reason="device revoked")
    await rig.hub.detach_device(rig.device)

    assert rig.hub.device_online(rig.device_id) is False
    assert [frame["device"]["online"] for frame in frames_of(rig.app)] == [False]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_protocol_error_close_still_gets_the_grace(tmp_path: Path) -> None:
    """Only a credential verdict is final. 1008 says the frame was wrong, not that it is gone."""
    rig = await hub_rig(tmp_path, offline_grace=GRACE)

    await rig.device.stop(code=CLOSE_PROTOCOL_ERROR, reason="first frame must be a hello")
    await rig.hub.detach_device(rig.device)

    assert rig.hub.device_online(rig.device_id) is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_request_during_the_grace_reaches_the_replacement(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=GRACE)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    frames_of(rig.app)
    await rig.hub.detach_device(rig.device)

    replacement = fake_device(rig.device_id)
    sent = _spawn(
        rig.hub.handle_app_frame(
            rig.app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
        )
    )
    await _yield_until(lambda: not sent.done())
    assert frames_of(replacement) == []

    await rig.hub.attach_device(replacement)
    await sent

    forwarded = frames_of(replacement)
    assert [frame["type"] for frame in forwarded] == ["session.send"]
    assert forwarded[0]["id"] == "r1"
    assert [frame.get("error") for frame in frames_of(rig.app)] == []
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_request_outliving_the_grace_is_answered_device_offline(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, offline_grace=GRACE)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    frames_of(rig.app)
    await rig.hub.detach_device(rig.device)

    started = time.perf_counter()
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.send", "id": "r2", "session_id": SESSION_ID, "text": "hi"}
    )
    waited = time.perf_counter() - started

    assert waited >= GRACE, f"the request was refused after {waited:.3f}s, before the grace ended"
    replies = [frame for frame in frames_of(rig.app) if frame.get("id") == "r2"]
    assert len(replies) == 1
    assert replies[0]["error"]["code"] == "device_offline"
    await rig.hub.stop()


def test_the_grace_period_is_visible_end_to_end(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            assert drain_until(app, "device.updated")["device"]["online"] is True

        # The socket is gone and the device is still online, then the period ends.
        assert state.hub.device_online(enrolled["device_id"]) is True
        assert drain_until(app, "device.updated")["device"]["online"] is False

        # A reconnect afterwards is an ordinary connect, and a second drop graces again.
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            assert drain_until(app, "device.updated")["device"]["online"] is True
        assert state.hub.device_online(enrolled["device_id"]) is True


def test_a_reconnect_inside_the_grace_never_shows_an_offline_device(
    client: TestClient, auth: dict[str, str]
) -> None:
    enrolled = enroll_device(client, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            drain_until(app, "device.updated")
        with client.websocket_connect("/ws/device", headers=headers) as device:
            device.send_json(device_hello())
            device.receive_json()
            time.sleep(GRACE * 3)
            # A marker no earlier frame can match, so everything the drop and the reconnect
            # produced is queued ahead of it and the assertion below sees all of it.
            minted = client.post("/api/devices/pairing", headers=auth).json()["code"]
            seen = _collect_until_code(app, minted)
    updates = [frame for frame in seen if frame.get("type") == "device.updated"]
    assert [frame["device"]["online"] for frame in updates] == [True]


# ---- refused upgrades ----


def test_a_refused_upgrade_is_logged_once_per_address_per_window() -> None:
    log = RejectionLog(window=60.0, flood_attempts=20)
    assert log.record("10.0.0.1", now=0.0).report == 1
    assert [log.record("10.0.0.1", now=1.0 + item).report for item in range(4)] == [None] * 4
    # A different address is its own bucket and reports immediately.
    assert log.record("10.0.0.2", now=2.0).report == 1
    # The next window carries what the last one swallowed, so the line says how steady it is.
    assert log.record("10.0.0.1", now=61.0).report == 5
    assert log.record("10.0.0.1", now=62.0).report is None


def test_an_address_far_past_any_backoff_is_refused_before_the_handshake() -> None:
    log = RejectionLog(window=60.0, flood_attempts=3)
    assert [log.record("10.0.0.1", now=float(item)).flooding for item in range(6)] == [
        False,
        False,
        False,
        True,
        True,
        True,
    ]
    # A new window starts over: a device that backed off properly still sees its close code.
    assert log.record("10.0.0.1", now=90.0).flooding is False


def test_the_rejection_log_stays_bounded() -> None:
    log = RejectionLog(window=1.0, max_addresses=8)
    for item in range(200):
        log.record(f"10.0.0.{item}", now=float(item))
    assert len(log._buckets) <= 8


def test_rejected_device_upgrades_are_reported_once(
    client: TestClient, auth: dict[str, str], state: GatewayState
) -> None:
    from .conftest import close_code_for

    headers = {"Authorization": "Bearer not-a-real-token"}
    for _ in range(3):
        assert close_code_for(client, "/ws/device", headers=headers) == CLOSE_UNAUTHORIZED
    # One line went out; the other two were counted against the same address and stayed silent.
    assert len(state.device_rejects._buckets) == 1
    bucket = next(iter(state.device_rejects._buckets.values()))
    assert bucket.suppressed == 2


# ---- helpers ----


def _collect_until_code(ws: Any, code: str) -> list[dict[str, Any]]:
    """Read frames up to and including the `pairing.progress` carrying this exact code."""
    seen: list[dict[str, Any]] = []
    for _ in range(40):
        frame = dict(ws.receive_json())
        seen.append(frame)
        if frame.get("type") == "pairing.progress" and frame.get("code") == code:
            return seen
    raise AssertionError(f"marker never arrived; saw {[f.get('type') for f in seen]}")


def _spawn(coroutine: Any) -> Any:
    import asyncio

    return asyncio.ensure_future(coroutine)


async def _yield_until(predicate: Any, ticks: int = 20) -> None:
    import asyncio

    for _ in range(ticks):
        await asyncio.sleep(0)
        if predicate():
            return


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    import asyncio

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
