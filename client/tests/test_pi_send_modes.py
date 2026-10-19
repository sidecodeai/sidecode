"""A message sent while a pi turn runs keeps its images on every path the hub takes.

The hub, a real `PiRunner` and the fake `pi --mode rpc` peer of
`test_pi_runner.py`: `auto` steers the running turn, `queue` holds the message
until the turn ends, and `interrupt` stops the turn and sends it. Each one hands
pi the images, and a file pi cannot take is refused rather than left behind.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from rc_client.agents.pi.adapter import PiRunner
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.helpers import drained
from tests.test_pi_runner import SESSION, Peer, Recorder, shim, wait_for
from tests.uploads import upload


@dataclass(slots=True)
class Running:
    """A hub driving one pi session whose turn has not finished."""

    hub: SessionHub
    entry: SessionEntry
    recorder: Recorder
    peer: Peer


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    hub = SessionHub(registry, recorder, "d1", lambda: [])
    session = Session(session_id=SESSION, device_id="d1", agent="pi", cwd=str(tmp_path))
    registry.upsert_session(session)
    entry = SessionEntry(session=session, channel=SessionChannel(registry, session, recorder))
    binary, peer = shim(tmp_path, "stall")

    async def on_turn_end() -> None:
        hub.schedule_drain(entry)

    entry.runner = PiRunner(
        entry.channel,
        binary=binary,
        cwd=str(tmp_path),
        session_id=SESSION,
        on_turn_end=on_turn_end,
    )
    hub.entries[SESSION] = entry
    await entry.runner.start()
    await hub.send({"id": "first", "session_id": SESSION, "text": "Reply with exactly OK"})
    await wait_for(lambda: recorder.events("assistant_text"))
    try:
        yield Running(hub, entry, recorder, peer)
    finally:
        await hub.close()
        registry.close()


def images_sent(peer: Peer) -> list[object]:
    """The `images` of every prompt pi was given, the first one's included."""
    return [command.get("images") for command in peer.of("prompt")]


async def test_auto_steers_the_running_turn_with_the_image(running: Running) -> None:
    image = upload()
    result = await running.hub.send(
        {"id": "steer", "session_id": SESSION, "text": "and this one", "attachments": [image]}
    )
    assert result == {"accepted": "steered"}
    steered = running.peer.of("prompt")[-1]
    assert steered["streamingBehavior"] == "steer"
    assert steered["images"] == [
        {"type": "image", "data": image["data_base64"], "mimeType": "image/png"}
    ]


async def test_auto_refuses_a_file_pi_cannot_take_rather_than_steer_without_it(
    running: Running,
) -> None:
    with pytest.raises(RcError) as caught:
        await running.hub.send(
            {
                "id": "steer",
                "session_id": SESSION,
                "text": "and read this",
                "attachments": [upload("notes.pdf", "application/pdf")],
            }
        )
    assert caught.value.code == "unsupported"
    assert images_sent(running.peer) == [None]
    assert running.entry.queue == []


async def test_queue_holds_the_image_until_the_turn_ends(running: Running) -> None:
    image = upload()
    result = await running.hub.send(
        {
            "id": "later",
            "session_id": SESSION,
            "text": "then this",
            "attachments": [image],
            "mode": "queue",
        }
    )
    assert result == {"accepted": "queued", "queued_id": "later"}
    await running.hub.stop({"session_id": SESSION})
    await drained(running.entry)
    await wait_for(lambda: len(running.peer.of("prompt")) == 2)
    assert images_sent(running.peer)[-1] == [
        {"type": "image", "data": image["data_base64"], "mimeType": "image/png"}
    ]
    assert running.recorder.events("error") == []


async def test_interrupt_stops_the_turn_and_sends_the_image(running: Running) -> None:
    image = upload()
    result = await running.hub.send(
        {
            "id": "now",
            "session_id": SESSION,
            "text": "stop and look",
            "attachments": [image],
            "mode": "interrupt",
        }
    )
    assert result == {"accepted": "sent"}
    assert "abort" in running.peer.names()
    assert images_sent(running.peer)[-1] == [
        {"type": "image", "data": image["data_base64"], "mimeType": "image/png"}
    ]
