"""A message held for an attached terminal goes out before one sent after it (A19, A49).

The terminal's turn was running, so the device held the message; then the
terminal went away and somebody sent another. That send resumes the session,
and the held message has to reach the agent first, with the files it came with.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from rc_client.models import AgentInfo, Choice
from rc_client.registry import Registry
from rc_client.sessions.attach import Attachment
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.helpers import drain, load_fixture, tick
from tests.test_hub import FakeRunner

HELD = "3405643a-4fec-404b-bff3-ab035776e59a"
LATER = "cd577919-f91d-4d55-8e61-f6bf2f41339e"


class FakeAttachment(Attachment):
    """A bridge that takes every injection, until the terminal leaves."""

    def __init__(self) -> None:
        super().__init__(session_id="sess-1", cwd="/repo", pid=4242, claude_version="2.1.267")
        self.injected: list[str] = []

    async def inject(self, message_id: str, text: str) -> bool:
        self.injected.append(text)
        return True

    async def relay_permission(self, request_id: str, behavior: str) -> bool:
        return True

    def detach(self) -> None:
        return None


def agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["takeover", "interrupt", "queue", "attachments", "history"],
            attach="channel",
            attach_ready=True,
            shared_attachments=True,
        )
    ]


@dataclass(slots=True)
class Detached:
    """A session whose terminal left while the device held a message for it."""

    hub: SessionHub
    entry: SessionEntry
    upload: list[dict[str, Any]]
    runners: list[FakeRunner]


@pytest.fixture
async def detached(tmp_path: Path) -> AsyncIterator[Detached]:
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    hub = SessionHub(registry, publish, "dev-1", agents)
    attachment = FakeAttachment()
    await hub.attach_registered(attachment)
    entry = hub.entry("sess-1")
    await tick(hub, entry, running=True)
    upload = load_fixture("app/session.send.json")["attachments"]
    held = await hub.send(
        {"id": HELD, "session_id": "sess-1", "text": "held", "attachments": upload}
    )
    assert held == {"accepted": "queued", "queued_id": HELD}
    await hub.shared.closed(entry, attachment, "none")
    assert entry.session.control == "none"
    runners: list[FakeRunner] = []

    async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
        runner = FakeRunner(entry.channel)
        runners.append(runner)
        return runner

    hub._build_runner = build  # type: ignore[method-assign]
    try:
        yield Detached(hub, entry, upload, runners)
    finally:
        await hub.close()
        registry.close()


@pytest.mark.parametrize("mode", ["auto", "queue"])
async def test_the_held_message_goes_out_before_a_send_after_the_detachment(
    detached: Detached, mode: str
) -> None:
    hub, entry = detached.hub, detached.entry
    result = await hub.send({"id": LATER, "session_id": "sess-1", "text": "later", "mode": mode})

    assert result == {"accepted": "queued", "queued_id": LATER}
    [runner] = detached.runners
    assert (runner.block_ids, runner.sources) == ([HELD], ["queue"])
    # Its files went when the terminal left; the bytes it kept write them again.
    assert runner.attachments == [detached.upload]
    assert [item["id"] for item in entry.queue] == [LATER]

    await runner.finish()
    await drain(hub, entry)
    assert runner.block_ids == [HELD, LATER]
