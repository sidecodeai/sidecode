"""``Hub.send_session_as``: a normal ``session.send`` for an account, with no socket behind it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..conftest import HubRig, fake_app, frames_of, hub_rig, session_summary

SESSION = "cccccccc-0000-4000-8000-000000000001"


async def _rig(tmp_path: Path, **options: Any) -> HubRig:
    rig = await hub_rig(tmp_path, **options)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION, rig.device_id)},
    )
    frames_of(rig.app)
    return rig


async def _answer(rig: HubRig, ok: bool = True) -> dict[str, Any]:
    for _ in range(200):
        sends = [f for f in frames_of(rig.device) if f.get("type") == "session.send"]
        if sends:
            request = sends[0]
            reply: dict[str, Any] = {"type": "reply", "id": request["id"], "from": request["from"]}
            if ok:
                reply.update(ok=True, result={"accepted": "sent"})
            else:
                reply.update(ok=False, error={"code": "busy", "message": "x"})
            await rig.hub.handle_device_frame(rig.device, reply)
            return request
        await asyncio.sleep(0.01)
    raise AssertionError("nothing reached the device")


async def test_it_forwards_like_an_app_send_and_returns_the_reply(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    sending = asyncio.create_task(rig.hub.send_session_as("admin", SESSION, "hello"))
    request = await _answer(rig)
    assert request["session_id"] == SESSION
    assert request["device_id"] == rig.device_id
    assert request["text"] == "hello"
    assert request["mode"] == "auto"
    assert request["from"].startswith("internal:")
    reply = await sending
    assert reply["ok"] is True
    assert reply["result"] == {"accepted": "sent"}
    assert rig.hub._pending == {}
    assert rig.hub._sinks == {}
    await rig.hub.stop()


async def test_it_never_attaches_evicts_or_writes_to_app_connections(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    apps = [rig.app, *(fake_app() for _ in range(7))]
    for app in apps[1:]:
        await rig.hub.attach_app(app)
    assert rig.hub.app_count == 8
    for app in apps:
        frames_of(app)

    sending = asyncio.create_task(rig.hub.send_session_as("admin", SESSION, "hi"))
    await _answer(rig)
    await sending

    assert rig.hub.app_count == 8
    assert all(not app.closed and app.close_code is None for app in apps)
    assert all(frames_of(app) == [] for app in apps)
    await rig.hub.stop()


async def test_another_account_and_unknown_sessions_get_the_same_refusal(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    stranger = await rig.hub.send_session_as("mallory", SESSION, "hi")
    missing = await rig.hub.send_session_as("admin", "no-such-session", "hi")
    assert stranger["ok"] is False and missing["ok"] is False
    assert stranger["error"] == missing["error"]
    assert stranger["error"]["code"] == "not_found"
    assert frames_of(rig.device) == []
    await rig.hub.stop()


async def test_a_silent_device_ends_in_a_timeout_reply(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, request_timeout=0.05)
    reply = await rig.hub.send_session_as("admin", SESSION, "hi")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "timeout"
    assert rig.hub._pending == {}
    await rig.hub.stop()


async def test_an_offline_device_is_reported(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, offline_grace=0.05)
    await rig.hub.detach_device(rig.device)
    reply = await rig.hub.send_session_as("admin", SESSION, "hi")
    assert reply["error"]["code"] == "device_offline"
    await rig.hub.stop()


async def test_a_device_error_comes_back_as_the_reply(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    sending = asyncio.create_task(rig.hub.send_session_as("admin", SESSION, "hi"))
    await _answer(rig, ok=False)
    assert (await sending)["error"]["code"] == "busy"
    await rig.hub.stop()


async def test_session_tail_returns_the_newest_events_in_order(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    for seq in range(1, 6):
        await rig.hub.handle_device_frame(
            rig.device,
            {
                "type": "session.event",
                "session_id": SESSION,
                "event": {"seq": seq, "kind": "assistant_text", "text": "x", "done": True},
            },
        )
    assert [e["seq"] for e in rig.hub.session_tail(SESSION, 3)] == [3, 4, 5]
    assert rig.hub.session_tail("other", 3) == []
    await rig.hub.stop()
