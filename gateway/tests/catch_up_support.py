"""What the catch-up tests share: a hub rig, an app that is behind, and a device that answers.

The tests drive the hub directly with fake connections whose queues are read by hand, so the order
of every frame is the order the hub wrote (A56).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from rc_gateway.connections import AppConnection, DeviceConnection

from .conftest import HubRig, device_hello, fake_device, frames_of, session_summary

SESSION_ID = "eeee1111-2222-4333-8444-555555555555"
OTHER_ID = "ffff1111-2222-4333-8444-555555555555"
#: Long enough that a positive test never races it.
PATIENT = 5.0
#: Short enough that a test about an unanswered fetch does not sit around.
IMPATIENT = 0.15


def block(seq: int, text: str | None = None) -> dict[str, Any]:
    return {
        "seq": seq,
        "ts": seq,
        "kind": "assistant_text",
        "block_id": f"b{seq}",
        "text": text if text is not None else f"t{seq}",
        "done": True,
    }


def blocks(first: int, last: int) -> list[dict[str, Any]]:
    return [block(seq) for seq in range(first, last + 1)]


def live(seq: int, session_id: str = SESSION_ID) -> dict[str, Any]:
    return {"type": "session.event", "session_id": session_id, "event": block(seq)}


def history_reply(
    request: dict[str, Any], events: list[dict[str, Any]], *, has_more: bool = False
) -> dict[str, Any]:
    return {
        "type": "reply",
        "id": request["id"],
        "from": request["from"],
        "ok": True,
        "result": {"events": events, "has_more": has_more},
    }


def refusal(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "reply",
        "id": request["id"],
        "from": request["from"],
        "ok": False,
        "error": {"code": "not_found", "message": "unknown session"},
    }


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.005)


class Watching:
    """Everything an app connection was sent, kept in order across reads."""

    def __init__(self, app: AppConnection) -> None:
        self.app = app
        self.frames: list[dict[str, Any]] = []

    def read(self) -> list[dict[str, Any]]:
        self.frames.extend(frames_of(self.app))
        return self.frames

    def events(self, session_id: str = SESSION_ID) -> list[int]:
        return [
            frame["event"]["seq"]
            for frame in self.read()
            if frame["type"] == "session.event" and frame["session_id"] == session_id
        ]

    def reply(self, identifier: str) -> dict[str, Any]:
        (frame,) = [item for item in self.read() if item.get("id") == identifier]
        return frame

    async def wait_reply(self, identifier: str, timeout: float = 2.0) -> dict[str, Any]:
        """The reply to a request, once it is there: a subscribe that waits for a device is
        answered after the call that sent it has returned."""
        await until(lambda: any(item.get("id") == identifier for item in self.read()), timeout)
        return self.reply(identifier)


_UNREAD: dict[str, list[dict[str, Any]]] = {}


async def next_request(
    device: DeviceConnection, kind: str, timeout: float = 2.0
) -> dict[str, Any] | None:
    """The next frame of ``kind`` the hub queued for a device, or None if none arrives.

    The queue is read in one go, so a frame that was not asked for yet is kept for the next call.
    """
    unread = _UNREAD.setdefault(device.id, [])
    deadline = time.monotonic() + timeout
    while True:
        unread.extend(frames_of(device))
        for index, frame in enumerate(unread):
            if frame.get("type") == kind:
                del unread[index]
                return frame
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.005)


async def history_request(device: DeviceConnection, timeout: float = 2.0) -> dict[str, Any] | None:
    return await next_request(device, "session.history", timeout)


async def put_session(
    rig: HubRig,
    *,
    last_seq: int,
    session_id: str = SESSION_ID,
    device: DeviceConnection | None = None,
) -> None:
    """What a device's summary leaves in the index, as the gateway knew a session before."""
    owner = device or rig.device
    await rig.hub.handle_device_frame(
        owner,
        {
            "type": "session.updated",
            "session": session_summary(session_id, owner.device_id, last_seq=last_seq),
        },
    )


async def subscribe(
    rig: HubRig,
    app: AppConnection | None = None,
    *,
    since: int | None = None,
    session_id: str = SESSION_ID,
    identifier: str = "s1",
) -> None:
    frame: dict[str, Any] = {
        "type": "session.subscribe",
        "id": identifier,
        "session_id": session_id,
    }
    if since is not None:
        frame["since_seq"] = since
    await rig.hub.handle_app_frame(app or rig.app, frame)


async def reconnect(rig: HubRig, *, last_seq: int) -> DeviceConnection:
    """The device's link comes back with its hello saying where its session has got to."""
    revived = fake_device(rig.device_id)
    await rig.hub.attach_device(revived)
    await rig.hub.handle_device_hello(
        revived,
        device_hello(sessions=[session_summary(SESSION_ID, rig.device_id, last_seq=last_seq)]),
    )
    return revived


def slow_sends(rig: HubRig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every frame to an app takes a moment to queue, as when the account is checked first."""
    send = rig.hub._send_app

    async def slow(connection: AppConnection, frame: dict[str, Any]) -> None:
        await asyncio.sleep(0.01)
        await send(connection, frame)

    monkeypatch.setattr(rig.hub, "_send_app", slow)


async def serve_history(
    rig: HubRig, device: DeviceConnection, tips: dict[str, int], *, idle: float = 0.2
) -> None:
    """Answer the device's history requests, page by page, until it has asked for no more.

    Each session's events are the blocks up to its tip, and a page is as long as the request says.
    """
    while (request := await history_request(device, timeout=idle)) is not None:
        tip = tips[request["session_id"]]
        first = request["after_seq"] + 1
        last = min(first + request["limit"] - 1, tip)
        reply = history_reply(request, blocks(first, last), has_more=last < tip)
        await rig.hub.handle_device_frame(device, reply)
