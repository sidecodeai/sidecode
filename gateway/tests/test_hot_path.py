"""Nothing on the path from a device frame to the apps waits on the disk or on a notification.

The device socket reads and dispatches one frame at a time, so anything awaited while handling a
frame is latency every later frame from that device inherits. These tests make the slow parts
unmistakably slow and assert the frames still come out fast, and that the shortcuts taken to get
there never let an app read a summary older than an event it has already been sent.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.hub import SessionTransition
from rc_gateway.index import SessionIndex

from .conftest import HubRig, frames_of, hub_rig, session_summary

SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
#: Long enough that a single one of them dwarfs the whole run when it is awaited in the wrong place.
SLOW_WRITE_SECONDS = 0.05
SLOW_PUSH_SECONDS = 0.3
EVENTS = 20


def _event(seq: int) -> dict[str, Any]:
    return {
        "type": "session.event",
        "session_id": SESSION_ID,
        "event": {"seq": seq, "kind": "message", "role": "assistant", "text": "ok"},
    }


def _summary(rig: HubRig, state: str) -> dict[str, Any]:
    return {
        "type": "session.updated",
        "session": session_summary(SESSION_ID, rig.device_id, state=state),
    }


def _slow_writes(index: SessionIndex, monkeypatch: pytest.MonkeyPatch) -> None:
    original = index._record_seqs

    def slow(batch: dict[str, int]) -> None:
        time.sleep(SLOW_WRITE_SECONDS)
        original(batch)

    monkeypatch.setattr(index, "_record_seqs", slow)


@pytest.mark.asyncio
async def test_fanning_an_event_out_does_not_wait_for_the_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path)
    _slow_writes(rig.index, monkeypatch)
    rig.app.subscribe(SESSION_ID, 0)

    started = time.perf_counter()
    for seq in range(1, EVENTS + 1):
        await rig.hub.handle_device_frame(rig.device, _event(seq))
    elapsed = time.perf_counter() - started

    assert [frame["event"]["seq"] for frame in frames_of(rig.app)] == list(range(1, EVENTS + 1))
    assert elapsed < SLOW_WRITE_SECONDS, f"{EVENTS} events took {elapsed:.3f}s"
    await rig.index.close()
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_summary_read_carries_an_event_the_disk_has_not_taken_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.handle_device_frame(rig.device, _summary(rig, "running"))
    _slow_writes(rig.index, monkeypatch)

    await rig.hub.handle_device_frame(rig.device, _event(9))

    indexed = await rig.index.get(SESSION_ID)
    assert indexed is not None
    assert indexed.last_seq == 9
    assert indexed.summary["last_seq"] == 9
    assert [summary["last_seq"] for summary in await rig.index.list_sessions()] == [9]
    await rig.index.close()
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_an_app_behind_a_live_event_is_told_to_resync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.handle_device_frame(rig.device, _summary(rig, "running"))
    _slow_writes(rig.index, monkeypatch)
    await rig.hub.handle_device_frame(rig.device, _event(9))
    # Replay buffers are bounded and the coldest is evicted, which leaves the index as the only
    # answer to "has this app fallen behind?". A cursor still on disk would answer "no".
    rig.hub._buffers.pop(SESSION_ID)

    await rig.hub.handle_app_frame(
        rig.app,
        {"type": "session.subscribe", "id": "s1", "session_id": SESSION_ID, "since_seq": 4},
    )

    reply = frames_of(rig.app)[-1]
    assert reply["id"] == "s1"
    assert reply["result"]["resync"] is True
    assert reply["result"]["session"]["last_seq"] == 9
    await rig.index.close()
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_slow_push_does_not_delay_the_next_device_frame(tmp_path: Path) -> None:
    delivered: list[str] = []

    async def slow_push(transition: SessionTransition) -> None:
        await asyncio.sleep(SLOW_PUSH_SECONDS)
        delivered.append(transition.state)

    rig = await hub_rig(tmp_path, on_session_transition=slow_push)
    await rig.hub.handle_device_frame(rig.device, _summary(rig, "idle"))

    started = time.perf_counter()
    await rig.hub.handle_device_frame(rig.device, _summary(rig, "needs_approval"))
    await rig.hub.handle_device_frame(rig.device, _summary(rig, "idle"))
    elapsed = time.perf_counter() - started

    assert elapsed < SLOW_PUSH_SECONDS, f"two frames took {elapsed:.3f}s"
    assert delivered == []
    await rig.hub.stop()
    assert delivered == ["needs_approval", "idle"]
    await rig.index.close()


@pytest.mark.asyncio
async def test_forwarding_a_request_does_not_read_the_session_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await hub_rig(tmp_path)
    await rig.hub.handle_device_frame(rig.device, _summary(rig, "idle"))
    # Every way the index can reach the file, so the assertion covers the whole store and not
    # just the one call this path used to make.
    reads: list[str] = []
    original = rig.index._connect

    def counted() -> sqlite3.Connection:
        reads.append("connect")
        return original()

    monkeypatch.setattr(rig.index, "_connect", counted)

    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
    )

    forwarded = frames_of(rig.device)[-1]
    assert forwarded["type"] == "session.send"
    assert forwarded["device_id"] == rig.device_id
    assert reads == []
    await rig.index.close()
    await rig.hub.stop()
