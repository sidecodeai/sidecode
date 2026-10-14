"""Amendment A43: every queue in `ts` order, and a queued message edited in place."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from rc_client.errors import RcError
from rc_client.sessions import queue as queue_module
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.queue import hold, snapshot_entry
from tests.helpers import event_validator, load_fixture
from tests.test_hub import FakeRunner, add_session, build_hub
from tests.test_shared_control import Harness

SHOT = {"name": "shot.png", "mime": "image/png", "data_base64": "aGk="}


def queue_of(*entries: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"id": name, "ts": ts} for name, ts in entries]


def ids(queue: list[dict[str, Any]]) -> list[str]:
    return [str(item["id"]) for item in queue]


async def send(hub: SessionHub, request_id: str, text: str, **fields: Any) -> dict[str, Any]:
    return await hub.send({"id": request_id, "session_id": "sess-1", "text": text, **fields})


def queue_events(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
    ]


def driven_session(hub: SessionHub) -> tuple[SessionEntry, FakeRunner]:
    """An idle session the device drives, on a fake agent."""
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    return entry, runner


async def drain_all(hub: SessionHub, entry: SessionEntry, runner: FakeRunner) -> None:
    """End one turn after another until the queue is empty."""
    while entry.queue:
        await runner.finish()
        await hub.drain_queue(entry)


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


# ------------------------------------------------------------- the ordered insert


@pytest.mark.parametrize(
    ("entries", "queue_ts", "expected"),
    [
        ((), 150, ["edit"]),
        ((("a", 100), ("b", 200), ("c", 300)), 50, ["edit", "a", "b", "c"]),
        ((("a", 100), ("b", 200), ("c", 300)), 150, ["a", "edit", "b", "c"]),
        ((("a", 100), ("b", 200), ("c", 300)), 350, ["a", "b", "c", "edit"]),
        ((("a", 100), ("b", 200), ("c", 200), ("d", 300)), 200, ["a", "b", "c", "edit", "d"]),
    ],
    ids=["empty", "front", "middle", "end", "after-equal"],
)
def test_a_message_with_queue_ts_is_held_in_front_of_every_later_entry(
    entries: tuple[tuple[str, int], ...], queue_ts: int, expected: list[str]
) -> None:
    queue = queue_of(*entries)
    item: dict[str, Any] = {"id": "edit"}
    hold(queue, item, queue_ts)
    assert ids(queue) == expected
    assert item["ts"] == queue_ts


def test_a_message_without_queue_ts_joins_the_end_stamped_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue_module, "now_ms", lambda: 5000)
    queue = queue_of(("a", 100), ("b", 200))
    item: dict[str, Any] = {"id": "new"}
    hold(queue, item, None)
    assert ids(queue) == ["a", "b", "new"]
    assert item["ts"] == 5000


def test_messages_queued_within_one_millisecond_never_share_a_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edited message comes back with its `ts` alone, so a tie would lose its place."""
    monkeypatch.setattr(queue_module, "now_ms", lambda: 5000)
    queue: list[dict[str, Any]] = []
    for name in ("a", "b", "c"):
        hold(queue, {"id": name}, None)
    assert [item["ts"] for item in queue] == [5000, 5001, 5002]

    edited: dict[str, Any] = {"id": "b-edited"}
    del queue[1]
    hold(queue, edited, 5001)
    assert ids(queue) == ["a", "b-edited", "c"]


def test_the_end_is_never_before_an_entry_with_a_later_ts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queue_module, "now_ms", lambda: 5000)
    queue = queue_of(("a", 100), ("b", 9000))
    hold(queue, {"id": "new"}, None)
    assert ids(queue) == ["a", "b", "new"]
    assert [item["ts"] for item in queue] == [100, 9000, 9001]


# ------------------------------------------------------------------ the request


@pytest.mark.parametrize("value", [-1, "12", 1.5, True])
async def test_a_queue_ts_that_is_not_a_non_negative_integer_is_a_bad_request(
    tmp_path: Path, value: Any
) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry, runner = driven_session(hub)
    await send(hub, "req-1", "first", mode="auto")

    with pytest.raises(RcError) as caught:
        await send(hub, "req-2", "second", mode="queue", queue_ts=value)
    assert caught.value.code == "bad_request"
    assert entry.queue == []
    assert runner.sent == ["first"]
    registry.close()


async def test_queue_ts_is_honoured_on_a_running_remote_session(tmp_path: Path) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry, runner = driven_session(hub)
    await send(hub, "req-1", "first", mode="auto")
    await send(hub, "req-a", "A", mode="auto")
    await send(hub, "req-b", "B", mode="queue")

    result = await send(hub, "req-x", "X", mode="queue", queue_ts=0)
    assert result == {"accepted": "queued", "queued_id": "req-x"}
    assert ids(queue_events(frames)[-1]["pending"]) == ["req-x", "req-a", "req-b"]
    assert entry.queue[0]["ts"] == 0

    await drain_all(hub, entry, runner)
    assert runner.sent == ["first", "X", "A", "B"]
    registry.close()


async def test_an_edited_message_goes_back_to_the_place_it_left(tmp_path: Path) -> None:
    """Queue A, B, C; take B out; send B' under B's `ts`: the line reads A, B', C."""
    hub, frames, registry = build_hub(tmp_path)
    entry, runner = driven_session(hub)
    await send(hub, "req-1", "first", mode="auto")
    for name in ("a", "b", "c"):
        await send(hub, f"req-{name}", name.upper(), mode="queue")
    b_ts = queue_events(frames)[-1]["pending"][1]["ts"]

    await hub.queue_remove({"session_id": "sess-1", "queued_id": "req-b"})
    result = await send(hub, "req-b2", "B, reworded", mode="queue", queue_ts=b_ts)

    assert result == {"accepted": "queued", "queued_id": "req-b2"}
    pending = queue_events(frames)[-1]["pending"]
    assert [(item["id"], item["text"]) for item in pending] == [
        ("req-a", "A"),
        ("req-b2", "B, reworded"),
        ("req-c", "C"),
    ]
    assert pending[1]["ts"] == b_ts
    assert entry.session.queued == 3

    await drain_all(hub, entry, runner)
    assert runner.sent == ["first", "A", "B, reworded", "C"]
    # The edited message goes out under its own request id (A12).
    assert runner.block_ids == ["req-1", "req-a", "req-b2", "req-c"]
    registry.close()


async def test_an_edit_that_outlived_its_turn_is_sent_at_once(tmp_path: Path) -> None:
    """`queue_ts` never decides whether a message queues: an idle session delivers it."""
    hub, _, registry = build_hub(tmp_path)
    entry, runner = driven_session(hub)
    result = await send(hub, "req-b2", "B, reworded", mode="queue", queue_ts=1788945028800)

    assert result == {"accepted": "queued", "queued_id": "req-b2"}
    assert runner.sent == ["B, reworded"]
    assert entry.queue == []
    registry.close()


# ----------------------------------------------------------------- the snapshot


async def test_the_snapshot_counts_files_only_for_an_entry_that_holds_them(
    tmp_path: Path,
) -> None:
    hub, frames, registry = build_hub(tmp_path)
    driven_session(hub)
    # Request ids are UUIDs on the wire, and the event is checked against the schema.
    plain, shots, no_files = (str(uuid.uuid4()) for _ in range(3))
    await send(hub, "req-1", "first", mode="auto")
    await send(hub, plain, "plain", mode="queue")
    await send(hub, shots, "two shots", mode="queue", attachments=[SHOT, SHOT])
    await send(hub, no_files, "no files", mode="queue", attachments=[])

    event = queue_events(frames)[-1]
    pending = event["pending"]
    assert ids(pending) == [plain, shots, no_files]
    assert pending[1]["attachments"] == 2
    assert "attachments" not in pending[0]
    assert "attachments" not in pending[2]
    validator = event_validator()
    if validator is not None:
        validator.validate(event)
    registry.close()


def test_snapshot_entries_read_as_the_frozen_fixture_does() -> None:
    fixture = load_fixture("events/queue.attachments.json")
    for expected in fixture["pending"]:
        files = [SHOT] * int(expected.get("attachments", 0))
        item = {**expected, "attachments": files, "source": "remote"}
        assert snapshot_entry(item) == expected


# ------------------------------------------------------- an attached terminal


async def test_queue_ts_is_honoured_on_a_busy_shared_session(harness: Harness) -> None:
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
    for name in ("a", "b", "c"):
        await send(harness.hub, f"req-{name}", name.upper())
    b_ts = harness.events("queue")[-1]["pending"][1]["ts"]

    await harness.hub.queue_remove({"session_id": "sess-1", "queued_id": "req-b"})
    result = await send(harness.hub, "req-b2", "B, reworded", mode="queue", queue_ts=b_ts)

    assert result == {"accepted": "queued", "queued_id": "req-b2"}
    assert harness.attachment.injected == []
    pending = harness.events("queue")[-1]["pending"]
    assert ids(pending) == ["req-a", "req-b2", "req-c"]
    assert pending[1]["ts"] == b_ts

    await harness.hub.shared.tick(entry, running=False)
    for _ in range(2):
        # One message is in flight at a time, until the transcript shows it.
        message_id = harness.attachment.injected[-1][0]
        await harness.hub.shared.echo_delivered(entry, message_id)
        await harness.hub.shared.tick(entry, running=False)
    assert [text for _, text in harness.attachment.injected] == ["A", "B, reworded", "C"]


async def deliver_the_rest(harness: Harness, entry: SessionEntry) -> None:
    """Let the transcript confirm each injection until nothing is held."""
    await harness.hub.shared.tick(entry, running=False)
    while entry.queue:
        await harness.hub.shared.echo_delivered(entry, harness.attachment.injected[-1][0])
        await harness.hub.shared.tick(entry, running=False)


@pytest.mark.parametrize("edit_first", [True, False], ids=["edit-then-absorb", "absorb-then-edit"])
async def test_an_absorbed_injection_goes_back_under_its_own_ts(
    harness: Harness, edit_first: bool
) -> None:
    """Queue A, B; take A out; B goes in and is absorbed; A' comes back: A' goes first."""
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
    await send(harness.hub, "req-a", "A")
    await send(harness.hub, "req-b", "B")
    a_ts = harness.events("queue")[-1]["pending"][0]["ts"]
    await harness.hub.queue_remove({"session_id": "sess-1", "queued_id": "req-a"})
    await harness.hub.shared.tick(entry, running=False)
    b_message = harness.attachment.injected[-1][0]

    if edit_first:
        # B is still in flight, so the edit is held.
        await send(harness.hub, "req-a2", "A, reworded", mode="queue", queue_ts=a_ts)
        await harness.hub.shared.echo_absorbed(entry, b_message)
    else:
        # Absorbed means the CLI was mid-turn, and the edit waits for that turn.
        await harness.hub.shared.echo_absorbed(entry, b_message)
        await harness.hub.shared.tick(entry, running=True)
        await send(harness.hub, "req-a2", "A, reworded", mode="queue", queue_ts=a_ts)

    assert ids(harness.events("queue")[-1]["pending"]) == ["req-a2", "req-b"]
    await deliver_the_rest(harness, entry)
    assert [text for _, text in harness.attachment.injected] == ["B", "A, reworded", "B"]


async def test_a_message_the_bridge_refused_goes_back_under_its_own_ts(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older edit that lands while the bridge is being asked stays in front."""
    entry = await harness.attach()
    await harness.hub.shared.tick(entry, running=True)
    await send(harness.hub, "req-a", "A")
    await send(harness.hub, "req-b", "B")
    a_ts = harness.events("queue")[-1]["pending"][0]["ts"]
    await harness.hub.queue_remove({"session_id": "sess-1", "queued_id": "req-a"})

    reachable = harness.attachment.inject
    asked: list[str] = []

    async def refuse_while_the_edit_lands(message_id: str, text: str) -> bool:
        asked.append(text)
        if len(asked) == 1:
            await send(harness.hub, "req-a2", "A, reworded", mode="queue", queue_ts=a_ts)
        return False

    monkeypatch.setattr(harness.attachment, "inject", refuse_while_the_edit_lands)
    await harness.hub.shared.tick(entry, running=False)

    assert asked == ["B", "A, reworded"]
    assert ids(entry.queue) == ["req-a2", "req-b"]
    assert ids(harness.events("queue")[-1]["pending"]) == ["req-a2", "req-b"]

    monkeypatch.setattr(harness.attachment, "inject", reachable)
    await deliver_the_rest(harness, entry)
    assert [text for _, text in harness.attachment.injected] == ["A, reworded", "B"]


async def test_an_edit_is_injected_at_once_into_an_idle_terminal(harness: Harness) -> None:
    entry = await harness.attach()
    result = await send(harness.hub, "req-b2", "B, reworded", mode="queue", queue_ts=1788945028800)

    assert result == {"accepted": "sent"}
    assert [text for _, text in harness.attachment.injected] == ["B, reworded"]
    assert entry.queue == []
