"""Recover turn boundaries from authoritative state without ending newer work."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from rc_client.agents.codex.daemon.session import CodexDaemonSession
from tests.test_codex_daemon_sessions import CHILD, THREAD, Harness, child_row, settle, started
from tests.test_codex_daemon_sessions import harness as harness


def runner_for(harness: Harness) -> CodexDaemonSession:
    runner = harness.hub.entry(THREAD).runner
    assert isinstance(runner, CodexDaemonSession)
    return runner


def completed_read(harness: Harness, **fields: Any) -> dict[str, Any]:
    row = copy.deepcopy(harness.daemon.replies["thread/list"]["data"][0])
    row["status"] = {"type": "idle"}
    turn = {"id": "parent-turn", "status": "completed", **fields}
    row["turns"] = [turn]
    harness.daemon.replies["thread/read"] = {"thread": row}
    return turn


async def parent_started(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.notify("turn/started", {"threadId": THREAD, "turn": {"id": "parent-turn"}})
    await settle(lambda: runner_for(harness).busy)


async def test_an_idle_loaded_child_does_not_hold_its_completed_parent(harness: Harness) -> None:
    await parent_started(harness)
    await harness.daemon.notify("thread/started", {"thread": child_row()})
    await settle(lambda: harness.service.child_parent(CHILD) == THREAD)
    await harness.daemon.notify("turn/completed", {"threadId": CHILD, "turn": {"id": "child-turn"}})
    await settle(lambda: not runner_for(harness)._children.working)
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, CHILD]}
    harness.daemon.replies["thread/read"] = {"thread": child_row(status={"type": "idle"})}
    await harness.service.refresh()
    assert not runner_for(harness)._children.working
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "idle")
    await harness.service.refresh()
    assert len(harness.events("turn_completed")) == 1
    assert not runner_for(harness).busy


async def test_a_loaded_active_child_still_holds_its_completed_parent(harness: Harness) -> None:
    await parent_started(harness)
    await harness.daemon.notify("thread/started", {"thread": child_row()})
    await settle(lambda: harness.service.child_parent(CHILD) == THREAD)
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, CHILD]}
    harness.daemon.replies["thread/read"] = {"thread": child_row()}
    await harness.service.refresh()
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await settle(lambda: runner_for(harness)._held is not None)
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.events("turn_completed") == []
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": CHILD, "status": {"type": "idle"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "idle")


async def test_an_active_parent_is_not_finished_by_an_idle_child_snapshot(
    harness: Harness,
) -> None:
    await parent_started(harness)
    await harness.daemon.notify("thread/started", {"thread": child_row()})
    await settle(lambda: harness.service.child_parent(CHILD) == THREAD)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await settle(lambda: runner_for(harness)._held is not None)
    harness.daemon.replies["thread/list"]["data"][0]["status"] = {"type": "active"}
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, CHILD]}
    harness.daemon.replies["thread/read"] = {"thread": child_row(status={"type": "idle"})}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.events("turn_completed") == []
    assert runner_for(harness)._turn_id is not None


@pytest.mark.parametrize(
    "status",
    [None, {"type": "systemError"}, {"type": "notLoaded"}, {"type": "unknown"}],
    ids=["missing", "system-error", "not-loaded", "unknown"],
)
async def test_an_unknown_child_status_does_not_release_its_held_parent(
    harness: Harness, status: dict[str, Any] | None
) -> None:
    await parent_started(harness)
    await harness.daemon.notify("thread/started", {"thread": child_row()})
    await settle(lambda: harness.service.child_parent(CHILD) == THREAD)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await settle(lambda: runner_for(harness)._held is not None)
    params: dict[str, Any] = {"threadId": CHILD}
    if status is not None:
        params["status"] = status
    await harness.daemon.notify("thread/status/changed", params)
    await asyncio.sleep(0.1)
    assert harness.hub.entry(THREAD).session.state == "running"
    assert runner_for(harness)._held is not None
    assert runner_for(harness)._children.working
    assert harness.events("turn_completed") == []


async def test_idle_recovers_a_real_turn_whose_completion_was_lost(harness: Harness) -> None:
    await parent_started(harness)
    completed_read(harness)
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": THREAD, "status": {"type": "idle"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "idle")
    assert harness.daemon.sent("thread/read")[-1] == {
        "threadId": THREAD,
        "includeTurns": True,
    }
    assert len(harness.events("turn_completed")) == 1


async def test_an_index_scan_recovers_a_lost_completion(harness: Harness) -> None:
    await parent_started(harness)
    completed_read(harness)
    harness.daemon.replies["thread/read"]["thread"]["status"]["extra"] = []
    harness.daemon.replies["thread/list"]["data"][0]["status"] = {"type": "idle"}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "idle"
    assert len(harness.events("turn_completed")) == 1


async def test_unknown_or_unfinished_state_cannot_end_a_turn(harness: Harness) -> None:
    await parent_started(harness)
    completed_read(harness, status="inProgress")
    harness.daemon.replies["thread/list"]["data"][0]["status"] = {"type": "idle"}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "running"
    harness.daemon.replies["thread/list"]["data"][0]["status"] = {"type": "systemError"}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.events("turn_completed") == []


async def test_idle_then_full_completion_ends_once_with_its_error(harness: Harness) -> None:
    await parent_started(harness)
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": THREAD, "status": {"type": "idle"}}
    )
    await harness.daemon.notify(
        "turn/completed",
        {
            "threadId": THREAD,
            "turn": {"id": "parent-turn", "status": "failed", "error": {"message": "failed"}},
        },
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "idle")
    await harness.service.refresh()
    assert len(harness.events("turn_completed")) == 1
    assert harness.events("turn_completed")[0]["stop_reason"] == "error"
    assert len(harness.events("error")) == 1


async def test_recovery_keeps_late_completion_error_and_usage(harness: Harness) -> None:
    await parent_started(harness)
    turn = completed_read(
        harness,
        status="failed",
        error={"message": "usage limit reached", "codexErrorInfo": "usageLimitExceeded"},
        durationMs=120,
    )
    await harness.daemon.notify(
        "thread/tokenUsage/updated",
        {"threadId": THREAD, "tokenUsage": {"total": {"inputTokens": 10, "outputTokens": 3}}},
    )
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": THREAD, "status": {"type": "idle"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "idle")
    await harness.daemon.notify("turn/completed", {"threadId": THREAD, "turn": turn})
    await asyncio.sleep(0.1)
    assert len(harness.events("turn_completed")) == 1
    completion = harness.events("turn_completed")[0]
    assert completion["stop_reason"] == "error"
    assert completion["duration_ms"] == 120
    assert completion["usage"]["input_tokens"] == 10
    assert completion["limit"] == {"resets_at": None}
    assert len(harness.events("error")) == 1


@pytest.mark.parametrize("status", [{"type": "active"}, {"type": "systemError"}, None])
async def test_recovery_requires_an_idle_thread_read(
    harness: Harness, status: dict[str, Any] | None
) -> None:
    await parent_started(harness)
    completed_read(harness)
    harness.daemon.replies["thread/read"]["thread"]["status"] = status
    harness.daemon.replies["thread/list"]["data"][0]["status"] = {"type": "idle"}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.events("turn_completed") == []


async def test_a_failed_recovery_read_keeps_the_turn_running(harness: Harness) -> None:
    await parent_started(harness)
    harness.daemon.errors["thread/read"] = "the thread is unavailable"
    harness.daemon.replies["thread/list"]["data"][0]["status"] = {"type": "idle"}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.events("turn_completed") == []


async def test_an_old_completion_read_cannot_end_a_new_turn(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await parent_started(harness)
    completed_read(harness)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.service.client.request

    async def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = await original(method, params, **kwargs)
        if method == "thread/read" and params.get("includeTurns"):
            snapshot = copy.deepcopy(result)
            entered.set()
            await release.wait()
            return snapshot
        return result

    monkeypatch.setattr(harness.service.client, "request", request)
    recovering = asyncio.create_task(
        runner_for(harness).reconcile_status(False, runner_for(harness).activity_epoch)
    )
    await entered.wait()
    try:
        await harness.daemon.notify(
            "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
        )
        await harness.daemon.notify(
            "turn/started", {"threadId": THREAD, "turn": {"id": "next-turn"}}
        )
        await settle(lambda: runner_for(harness)._turn_id == "next-turn")
    finally:
        release.set()
        await recovering
    assert harness.hub.entry(THREAD).session.state == "running"
    assert len(harness.events("turn_completed")) == 1


async def test_slow_subscription_preserves_notification_order(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.daemon.errors["thread/resume"] = "no rollout found for thread id"
    await started(harness, loaded=[THREAD])
    del harness.daemon.errors["thread/resume"]
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.service.client.request

    async def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if method == "thread/items/list":
            entered.set()
            await release.wait()
        return await original(method, params, **kwargs)

    monkeypatch.setattr(harness.service.client, "request", request)
    await harness.daemon.notify("turn/started", {"threadId": THREAD, "turn": {"id": "parent-turn"}})
    await entered.wait()
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    release.set()
    await settle(lambda: bool(harness.events("turn_completed")))
    assert harness.hub.entry(THREAD).session.state == "idle"
    assert len(harness.events("turn_started")) == 1
    assert len(harness.events("turn_completed")) == 1


async def test_an_idle_scan_does_not_end_a_pending_start(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await started(harness, loaded=[THREAD])
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.service.client.request

    async def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if method == "turn/start":
            entered.set()
            await release.wait()
        return await original(method, params, **kwargs)

    monkeypatch.setattr(harness.service.client, "request", request)
    sending = asyncio.create_task(
        harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    )
    await entered.wait()
    try:
        assert runner_for(harness).busy
        await harness.service.refresh()
        await harness.daemon.notify(
            "thread/status/changed", {"threadId": THREAD, "status": {"type": "idle"}}
        )
        await asyncio.sleep(0.1)
        assert harness.events("turn_completed") == []
    finally:
        release.set()
        await sending
    assert harness.hub.entry(THREAD).session.state == "running"


async def test_active_before_a_remote_start_response_keeps_its_trigger(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.service.client.request

    async def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if method == "turn/start":
            entered.set()
            await release.wait()
        return await original(method, params, **kwargs)

    monkeypatch.setattr(harness.service.client, "request", request)
    sending = asyncio.create_task(
        harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    )
    await entered.wait()
    try:
        await harness.daemon.notify(
            "thread/status/changed", {"threadId": THREAD, "status": {"type": "active"}}
        )
        await settle(lambda: bool(harness.events("turn_started")))
        await harness.daemon.notify("turn/started", {"threadId": THREAD, "turn": {"id": "turn-1"}})
        await settle(lambda: runner_for(harness)._turn_id == "turn-1")
    finally:
        release.set()
        await sending
    assert len(harness.events("turn_started")) == 1
    assert harness.events("turn_started")[0]["trigger"] == "remote"
    assert runner_for(harness).local_turn
    assert harness.hub.entry(THREAD).session.control == "remote"


async def test_an_old_idle_snapshot_cannot_end_a_new_turn(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await started(harness, loaded=[THREAD])
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.service.client.request

    async def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = await original(method, params, **kwargs)
        if method == "thread/list":
            snapshot = copy.deepcopy(result)
            entered.set()
            await release.wait()
            return snapshot
        return result

    monkeypatch.setattr(harness.service.client, "request", request)
    refresh = asyncio.create_task(harness.service.refresh())
    await entered.wait()
    try:
        await harness.daemon.notify(
            "turn/started", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
        )
        await settle(lambda: harness.hub.entry(THREAD).session.state == "running")
    finally:
        release.set()
        await refresh
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.events("turn_completed") == []


async def test_status_active_then_real_start_publishes_one_start(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": THREAD, "status": {"type": "active"}}
    )
    await harness.daemon.notify("turn/started", {"threadId": THREAD, "turn": {"id": "parent-turn"}})
    await settle(lambda: runner_for(harness)._turn_id == "parent-turn")
    assert len(harness.events("turn_started")) == 1


async def test_completion_drains_the_queue_outside_the_notification_lock(harness: Harness) -> None:
    await parent_started(harness)
    result = await harness.hub.send(
        {"id": "req-queue", "session_id": THREAD, "text": "next", "mode": "queue"}
    )
    assert result["accepted"] == "queued"
    harness.daemon.replies["turn/start"] = {"turn": {"id": "next-turn"}}
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await settle(lambda: runner_for(harness)._turn_id == "next-turn")
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await asyncio.sleep(0.1)
    assert harness.hub.entry(THREAD).session.state == "running"
    assert len(harness.events("turn_completed")) == 1


@pytest.mark.parametrize("finishes_early", [False, True], ids=["running", "completed"])
async def test_a_duplicate_completion_cannot_end_a_queued_start_without_its_response(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, finishes_early: bool
) -> None:
    await parent_started(harness)
    result = await harness.hub.send(
        {"id": "req-queue", "session_id": THREAD, "text": "next", "mode": "queue"}
    )
    assert result["accepted"] == "queued"
    harness.daemon.replies["turn/start"] = {"turn": {"id": "next-turn"}}
    entered = asyncio.Event()
    release = asyncio.Event()
    original = harness.service.client.request

    async def request(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        result = await original(method, params, **kwargs)
        if method == "turn/start":
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(harness.service.client, "request", request)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "parent-turn"}}
    )
    await entered.wait()
    runner = runner_for(harness)
    try:
        assert runner._turn_id is None
        assert runner._start_requests == 1
        epoch = runner._turn_epoch
        await runner.notification("turn/completed", {"turn": {"id": "parent-turn"}})
        assert runner._turn_epoch == epoch
        assert runner.busy
        assert not runner._turn_done.is_set()
        assert len(harness.events("turn_completed")) == 1
        if finishes_early:
            await runner.notification("turn/completed", {"turn": {"id": "next-turn"}})
            assert len(harness.events("turn_completed")) == 2
    finally:
        release.set()
    await settle(lambda: runner._start_requests == 0)
    runner = runner_for(harness)
    if finishes_early:
        assert not runner.busy
        assert harness.hub.entry(THREAD).session.state == "idle"
        assert len(harness.events("turn_started")) == 1
        assert len(harness.events("turn_completed")) == 2
    else:
        await settle(lambda: runner._turn_id == "next-turn")
        assert harness.hub.entry(THREAD).session.state == "running"
        assert len(harness.events("turn_started")) == 2
        assert len(harness.events("turn_completed")) == 1
