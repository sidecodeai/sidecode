"""Registry behaviour: seq persistence, history ordering, blocks, idempotency."""

from __future__ import annotations

from pathlib import Path

from rc_client.models import Session
from rc_client.registry import Registry


def make_session(session_id: str = "s1") -> Session:
    return Session(session_id=session_id, device_id="d1", agent="claude", cwd="/tmp")


def store(registry: Registry, session_id: str, **event: object) -> dict[str, object]:
    seq = registry.next_seq(session_id)
    payload = {"seq": seq, "ts": 0, **event}
    registry.store_event(session_id, payload, payload)
    return payload


def test_seq_is_monotonic_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    registry = Registry(path)
    registry.upsert_session(make_session())
    first = [registry.next_seq("s1") for _ in range(3)]
    registry.close()

    reopened = Registry(path)
    second = [reopened.next_seq("s1") for _ in range(2)]
    reopened.close()
    assert first == [1, 2, 3]
    assert second == [4, 5]


def test_history_returns_the_latest_event_per_block_ascending_by_seq(tmp_path: Path) -> None:
    """PROTOCOL.md section 8: one event per block, and `seq` must increase down the page."""
    registry = Registry(tmp_path / "state.sqlite3")
    registry.upsert_session(make_session())
    store(registry, "s1", kind="user_message", block_id="u1", text="go", source="remote")
    store(registry, "s1", kind="tool_call", block_id="t1", status="running")
    store(registry, "s1", kind="assistant_text", block_id="a1", text="hi", done=True)
    store(registry, "s1", kind="tool_call", block_id="t1", status="succeeded")

    events, has_more = registry.history("s1")
    assert not has_more
    assert [event["block_id"] for event in events] == ["u1", "a1", "t1"]
    assert [event["seq"] for event in events] == [1, 3, 4]
    assert events[-1]["status"] == "succeeded"
    registry.close()


def test_history_pages_backwards_with_before_seq(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    registry.upsert_session(make_session())
    for index in range(10):
        store(registry, "s1", kind="notice", level="info", text=f"n{index}")

    page, has_more = registry.history("s1", limit=4)
    assert has_more
    assert [event["seq"] for event in page] == [7, 8, 9, 10]
    older, has_more_older = registry.history("s1", before_seq=7, limit=4)
    assert has_more_older
    assert [event["seq"] for event in older] == [3, 4, 5, 6]
    registry.close()


def test_block_returns_the_untruncated_copy(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    registry.upsert_session(make_session())
    seq = registry.next_seq("s1")
    wire = {"seq": seq, "ts": 0, "kind": "tool_call", "block_id": "t1", "output": "short"}
    full = dict(wire, output="x" * 50_000)
    registry.store_event("s1", wire, full)

    stored = registry.block("s1", "t1")
    assert stored is not None
    assert len(stored["output"]) == 50_000
    assert registry.block("s1", "missing") is None
    registry.close()


def test_requests_are_recalled_for_idempotency(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    assert registry.recall_request("s1", "r1") is None
    registry.remember_request("s1", "r1", {"accepted": "sent"})
    assert registry.recall_request("s1", "r1") == {"accepted": "sent"}
    registry.close()


def test_rekey_moves_sessions_events_and_requests(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    registry.upsert_session(make_session("pending-1"))
    store(registry, "pending-1", kind="notice", level="info", text="hello")
    registry.remember_request("pending-1", "r1", {"accepted": "sent"})

    registry.rekey_session("pending-1", "real-1")
    events, _ = registry.history("real-1")
    assert len(events) == 1
    assert registry.recall_request("real-1", "r1") == {"accepted": "sent"}
    assert registry.history("pending-1") == ([], False)
    registry.close()


# ------------------------------------- writes leave the event loop alone


async def test_writes_are_deferred_and_applied_in_one_batch(tmp_path: Path) -> None:
    """A commit is a disk flush; the loop must not wait for one per event."""
    import asyncio
    import sqlite3

    path = tmp_path / "state.sqlite3"
    registry = Registry(path)
    reader = sqlite3.connect(str(path))
    try:
        registry.upsert_session(make_session())
        for index in range(20):
            store(registry, "s1", kind="assistant_text", block_id=f"b{index}")
        stored = reader.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert stored == 0, "nothing was written while the loop was running"

        async def landed() -> None:
            while reader.execute("SELECT COUNT(*) FROM events").fetchone()[0] < 20:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(landed(), timeout=5)
    finally:
        reader.close()
        registry.close()


async def test_a_read_sees_a_write_that_has_not_landed_yet(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    try:
        registry.upsert_session(make_session())
        store(registry, "s1", kind="assistant_text", block_id="b1")
        events, _ = registry.history("s1")
        assert [event["block_id"] for event in events] == ["b1"]
        assert [session.session_id for session in registry.load_sessions()] == ["s1"]
        registry.set_kv("offset", "42")
        assert registry.get_kv("offset") == "42"
    finally:
        registry.close()


def test_closing_applies_everything_still_queued(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    registry = Registry(path)
    registry.upsert_session(make_session())
    store(registry, "s1", kind="assistant_text", block_id="b1")
    registry.close()

    reopened = Registry(path)
    try:
        events, _ = reopened.history("s1")
        assert [event["block_id"] for event in events] == ["b1"]
        assert reopened.next_seq("s1") == 2
    finally:
        reopened.close()


def test_pruning_keeps_the_newest_events_without_counting_the_rest(tmp_path: Path) -> None:
    import rc_client.registry as registry_module

    registry = Registry(tmp_path / "state.sqlite3")
    try:
        registry.upsert_session(make_session())
        for index in range(30):
            store(registry, "s1", kind="assistant_text", block_id=f"b{index}")
        registry.flush()
        registry._prune_to("s1", 10)
        registry.flush()
        events, _ = registry.history("s1", limit=1000)
        assert [event["block_id"] for event in events] == [f"b{index}" for index in range(20, 30)]
        # And a session with less history than the bound loses nothing.
        registry._prune_to("s1", 10_000)
        events, _ = registry.history("s1", limit=1000)
        assert len(events) == 10
        assert registry_module.MAX_EVENTS_PER_SESSION > 0
    finally:
        registry.close()
