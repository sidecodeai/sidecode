"""A request left on a device link that was then replaced is answered soon, not after 60 s (A56)."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.frames import CLOSE_DEVICE_REPLACED
from rc_gateway.hub import SessionTransition

from .conftest import HubRig, device_hello, fake_device, frames_of, hub_rig, session_summary

SESSION_ID = "dddd1111-2222-4333-8444-555555555555"
#: Far longer than the test runs: a failure at the timeout would not show up here.
REQUEST_TIMEOUT = 30.0
#: What the new link is given to answer in, in these tests.
GRACE = 0.2


async def _rig_with_a_request_in_flight(
    tmp_path: Path, *, grace: float = GRACE, **options: Any
) -> HubRig:
    rig = await hub_rig(tmp_path, request_timeout=REQUEST_TIMEOUT, orphan_grace=grace, **options)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    frames_of(rig.app)
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
    )
    assert [frame["type"] for frame in frames_of(rig.device)] == ["session.send"]
    return rig


def _replies(rig: HubRig, identifier: str) -> list[dict[str, Any]]:
    return [frame for frame in frames_of(rig.app) if frame.get("id") == identifier]


async def _replied(rig: HubRig, identifier: str, timeout: float = 2.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    seen: list[dict[str, Any]] = []
    while not seen and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
        seen = _replies(rig, identifier)
    return seen


@pytest.mark.asyncio
async def test_what_the_old_link_never_answered_is_failed_once_the_new_one_has_had_its_turn(
    tmp_path: Path,
) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)

    await rig.hub.detach_device(rig.device)
    started = time.perf_counter()
    await rig.hub.attach_device(fake_device(rig.device_id))
    assert _replies(rig, "r1") == []

    replies = await _replied(rig, "r1")
    assert len(replies) == 1
    assert replies[0]["ok"] is False
    assert replies[0]["error"]["code"] == "timeout"
    assert GRACE <= time.perf_counter() - started < REQUEST_TIMEOUT / 10
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_request_the_device_answers_on_the_new_link_is_delivered(tmp_path: Path) -> None:
    """A `session.create` that outlives a link flap succeeds, as it does without this change."""
    rig = await _rig_with_a_request_in_flight(tmp_path)
    await rig.hub.detach_device(rig.device)
    replacement = fake_device(rig.device_id)
    await rig.hub.attach_device(replacement)
    frames_of(rig.app)

    await rig.hub.handle_device_frame(
        replacement,
        {"type": "reply", "id": "r1", "from": rig.app.id, "ok": True, "result": {"accepted": 1}},
    )
    await asyncio.sleep(GRACE * 2)

    (answer,) = _replies(rig, "r1")
    assert answer["ok"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_reply_that_arrives_on_the_replaced_link_is_still_delivered(tmp_path: Path) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)
    await rig.hub.attach_device(fake_device(rig.device_id))
    frames_of(rig.app)

    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "reply", "id": "r1", "from": rig.app.id, "ok": True, "result": {"accepted": 1}},
    )

    (answer,) = _replies(rig, "r1")
    assert answer["ok"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_the_grace_starts_when_the_device_can_answer_on_the_new_link(
    tmp_path: Path,
) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)
    await rig.hub.detach_device(rig.device)
    replacement = fake_device(rig.device_id)
    replacement.hold_until_greeted()
    await rig.hub.attach_device(replacement)

    # Longer than the grace, with the device still handshaking: it could not have answered yet.
    await asyncio.sleep(GRACE * 2)
    assert _replies(rig, "r1") == []

    await replacement.greet({"type": "hello_ack"})
    assert _replies(rig, "r1") == []
    replies = await _replied(rig, "r1")
    assert [reply["error"]["code"] for reply in replies] == ["timeout"]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_newer_connection_gives_what_the_one_it_replaced_never_answered_the_same_turn(
    tmp_path: Path,
) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)

    await rig.hub.attach_device(fake_device(rig.device_id))

    assert rig.device.close_code == CLOSE_DEVICE_REPLACED
    replies = await _replied(rig, "r1")
    assert [reply["error"]["code"] for reply in replies] == ["timeout"]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_each_newer_link_gives_the_request_its_own_grace(tmp_path: Path) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path, grace=0.5)
    await rig.hub.attach_device(fake_device(rig.device_id))
    await asyncio.sleep(0.3)
    # The second link is replaced in its turn, before its own grace has run out.
    await rig.hub.attach_device(fake_device(rig.device_id))

    # Past the second link's grace, inside the third's.
    await asyncio.sleep(0.4)
    assert _replies(rig, "r1") == []

    replies = await _replied(rig, "r1")
    assert [reply["error"]["code"] for reply in replies] == ["timeout"]
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_request_written_to_the_new_connection_is_left_alone(tmp_path: Path) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)
    await rig.hub.detach_device(rig.device)
    replacement = fake_device(rig.device_id)
    await rig.hub.attach_device(replacement)
    frames_of(rig.app)

    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.send", "id": "r2", "session_id": SESSION_ID, "text": "again"}
    )
    (forwarded,) = frames_of(replacement)
    assert forwarded["id"] == "r2"
    await _replied(rig, "r1")

    await rig.hub.handle_device_frame(
        replacement,
        {"type": "reply", "id": "r2", "from": rig.app.id, "ok": True, "result": {"accepted": 1}},
    )
    (answer,) = _replies(rig, "r2")
    assert answer["ok"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_request_sent_again_with_the_same_id_is_not_failed_as_the_orphan(
    tmp_path: Path,
) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)
    await rig.hub.detach_device(rig.device)
    replacement = fake_device(rig.device_id)
    await rig.hub.attach_device(replacement)
    frames_of(rig.app)

    # The app saw nothing and sends the same request again, which the new link takes.
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
    )
    assert [frame["id"] for frame in frames_of(replacement)] == ["r1"]
    await asyncio.sleep(GRACE * 2)
    assert _replies(rig, "r1") == []

    await rig.hub.handle_device_frame(
        replacement,
        {"type": "reply", "id": "r1", "from": rig.app.id, "ok": True, "result": {"accepted": 1}},
    )
    (answer,) = _replies(rig, "r1")
    assert answer["ok"] is True
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_session_the_person_just_closed_is_not_announced_when_its_device_returns(
    tmp_path: Path,
) -> None:
    """The close is still unanswered when the link drops; its last words are not news (A39)."""
    transitions: list[SessionTransition] = []

    async def record(transition: SessionTransition) -> None:
        transitions.append(transition)

    rig = await _rig_with_a_request_in_flight(tmp_path, on_session_transition=record)
    running = session_summary(SESSION_ID, rig.device_id, state="running")
    await rig.hub.handle_device_frame(rig.device, {"type": "session.updated", "session": running})
    await asyncio.sleep(0.05)
    transitions.clear()
    await rig.hub.handle_app_frame(
        rig.app,
        {"type": "session.archive", "id": "close-1", "session_id": SESSION_ID, "archived": True},
    )
    await rig.hub.detach_device(rig.device)
    replacement = fake_device(rig.device_id)
    await rig.hub.attach_device(replacement)

    # The device comes back reporting the end of the turn it was closing.
    await rig.hub.handle_device_hello(
        replacement,
        device_hello(sessions=[session_summary(SESSION_ID, rig.device_id, state="idle")]),
    )
    await rig.hub.stop()

    assert [(item.previous_state, item.state) for item in transitions] == []


@pytest.mark.asyncio
async def test_an_orphaned_request_is_logged_by_its_ids_and_never_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await _rig_with_a_request_in_flight(tmp_path)
    await rig.hub.detach_device(rig.device)

    with caplog.at_level(logging.INFO, logger="rc_gateway.hub"):
        await rig.hub.attach_device(fake_device(rig.device_id))
        await _replied(rig, "r1")

    (record,) = [item for item in caplog.records if "orphaned" in item.getMessage()]
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["device_id"] == rig.device_id
    assert fields["session_id"] == SESSION_ID
    assert fields["kind"] == "session.send"
    assert fields["outcome"] == "timeout"
    assert isinstance(fields["ms"], int)
    assert "hi" not in {str(value) for value in fields.values()}
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_the_shutdown_does_not_wait_for_the_grace_of_an_orphan(tmp_path: Path) -> None:
    rig = await hub_rig(tmp_path, request_timeout=REQUEST_TIMEOUT, orphan_grace=60.0)
    await rig.hub.handle_device_frame(
        rig.device,
        {"type": "session.updated", "session": session_summary(SESSION_ID, rig.device_id)},
    )
    await rig.hub.handle_app_frame(
        rig.app, {"type": "session.send", "id": "r1", "session_id": SESSION_ID, "text": "hi"}
    )
    await rig.hub.detach_device(rig.device)
    await rig.hub.attach_device(fake_device(rig.device_id))

    started = time.perf_counter()
    await rig.hub.stop()

    assert time.perf_counter() - started < 5.0
    assert rig.hub._orphan_timers == set()
