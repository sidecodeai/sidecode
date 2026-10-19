"""A message with files sent into a running turn keeps them, steered or queued.

`auto` steers a running turn, and a steer used to carry the words alone: the
files were dropped without a word. The hub now steers a message with files only
into a runner that takes them there (`AttachmentSteering`) and queues it for
any other runner, so the files go out with the words when the turn ends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.helpers import drain
from tests.test_hub import FakeRunner, add_session, build_hub
from tests.uploads import upload


class FileSteeringRunner(FakeRunner):
    """A runner whose agent takes files into a running turn, unless it refuses."""

    def __init__(self, channel: SessionChannel, *, refuse: bool = False) -> None:
        super().__init__(channel, steer=True)
        self.steered_files: list[list[dict[str, Any]]] = []
        self._refuse = refuse

    async def steer_with_attachments(
        self, text: str, attachments: list[dict[str, Any]], block_id: str | None = None
    ) -> bool:
        if self._refuse:
            return False
        self.steered.append(text)
        self.steered_files.append(attachments)
        self.steer_block_ids.append(block_id)
        return True


async def running(hub: SessionHub, runner_type: type[FakeRunner], **options: Any) -> FakeRunner:
    """A session whose first turn is still working on the given runner."""
    entry = add_session(hub, steer=True)
    runner = runner_type(entry.channel, **options)
    entry.runner = runner
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "first"})
    assert runner.busy
    return runner


def send_with(image: dict[str, Any], mode: str = "auto") -> dict[str, Any]:
    return {
        "id": "req-2",
        "session_id": "sess-1",
        "text": "and this",
        "attachments": [image],
        "mode": mode,
    }


async def test_a_runner_that_takes_files_mid_turn_is_steered_with_them(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    runner = await running(hub, FileSteeringRunner)
    image = upload()

    assert await hub.send(send_with(image)) == {"accepted": "steered"}
    assert isinstance(runner, FileSteeringRunner)
    assert (runner.steered, runner.steered_files) == (["and this"], [[image]])
    assert runner.steer_block_ids == ["req-2"]
    registry.close()


async def test_a_runner_that_steers_words_alone_gets_the_message_queued(
    tmp_path: Path,
) -> None:
    hub, _, registry = build_hub(tmp_path)
    runner = await running(hub, FakeRunner, steer=True)
    image = upload()

    assert await hub.send(send_with(image)) == {"accepted": "queued", "queued_id": "req-2"}
    assert runner.steered == []
    entry = hub.entry("sess-1")
    await deliver_next(hub, entry, runner)
    assert (runner.sent, runner.attachments) == (["first", "and this"], [[], [image]])
    registry.close()


async def test_a_steer_the_runner_refuses_is_queued_with_its_files(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    runner = await running(hub, FileSteeringRunner, refuse=True)
    image = upload()

    assert await hub.send(send_with(image)) == {"accepted": "queued", "queued_id": "req-2"}
    await deliver_next(hub, hub.entry("sess-1"), runner)
    assert runner.attachments == [[], [image]]
    registry.close()


@pytest.mark.parametrize(
    ("mode", "accepted"),
    [("queue", {"accepted": "queued", "queued_id": "req-2"}), ("interrupt", {"accepted": "sent"})],
)
async def test_queue_and_interrupt_still_deliver_the_files(
    tmp_path: Path, mode: str, accepted: dict[str, Any]
) -> None:
    hub, _, registry = build_hub(tmp_path)
    runner = await running(hub, FileSteeringRunner)
    image = upload()

    assert await hub.send(send_with(image, mode)) == accepted
    if mode == "queue":
        await deliver_next(hub, hub.entry("sess-1"), runner)
    assert isinstance(runner, FileSteeringRunner)
    assert runner.steered_files == []
    assert runner.attachments == [[], [image]]
    registry.close()


async def deliver_next(hub: SessionHub, entry: SessionEntry, runner: FakeRunner) -> None:
    """End the running turn, then hand the queue its next message."""
    await runner.finish()
    await drain(hub, entry)
