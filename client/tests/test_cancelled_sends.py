"""A49 and A39: a send stopped on its way to the agent leaves no files behind.

A close stops a drain that may be handing a message over at that moment. Its
files are written by then, and its bubble may be out, but the agent never gets
the prompt: the files go at once rather than at the age limit, the turn the
message opened ends, and the bubble is followed by a notice that it went unread.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_client import attachments
from rc_client.agents.claude.adapter import UNREAD, ClaudeRunner
from rc_client.agents.grok import catalog as grok_catalog
from rc_client.agents.grok.adapter import UNREAD as GROK_UNREAD
from rc_client.agents.grok.adapter import GrokRunner
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel

SHOT = {"name": "shot.png", "mime": "image/png", "data_base64": "aGVsbG8="}


class Published:
    """The frames a channel published, and a gate that can hold one kind back."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.hold: str | None = None
        self.held = asyncio.Event()

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        event = frame.get("event") or {}
        if self.hold is not None and event.get("state") == self.hold:
            self.held.set()
            await asyncio.Event().wait()

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]


def channel(tmp_path: Path, agent: str, published: Published) -> SessionChannel:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="sess-1", device_id="d", agent=agent, cwd=str(tmp_path))
    registry.upsert_session(session)
    return SessionChannel(registry, session, published)


def files_left() -> list[Path]:
    folder = attachments.state_root() / "sess-1"
    return sorted(folder.rglob("*.png")) if folder.is_dir() else []


async def cancelled(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class HangingSDK:
    """A CLI that never acknowledges the prompt it is being handed."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()

    async def query(self, prompt: str) -> None:
        self.asked.set()
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        return None


async def test_a_claude_send_stopped_while_the_prompt_goes_out_takes_its_files(
    tmp_path: Path,
) -> None:
    published = Published()
    runner = ClaudeRunner(channel(tmp_path, "claude", published), binary=None, cwd=str(tmp_path))
    sdk = HangingSDK()
    runner._client = sdk  # type: ignore[assignment]
    sending = asyncio.create_task(runner.send("look", [SHOT], source="queue", block_id="req-1"))
    await asyncio.wait_for(sdk.asked.wait(), timeout=2)
    assert files_left(), "the files are written before the CLI is asked"

    await cancelled(sending)

    assert files_left() == []
    assert runner.busy is False, "a later message is not held behind a turn that never ran"
    [bubble] = published.events("user_message")
    assert bubble["attachments"] == [{"name": "shot.png", "mime": "image/png", "size": 5}]
    assert [event["stop_reason"] for event in published.events("turn_completed")] == ["interrupted"]
    assert [(event["level"], event["text"]) for event in published.events("notice")] == [
        ("warn", UNREAD)
    ]
    runner.channel.registry.close()


async def test_a_claude_send_stopped_while_the_cli_restarts_takes_its_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new effort level restarts the CLI first, which is the long window."""
    published = Published()
    runner = ClaudeRunner(channel(tmp_path, "claude", published), binary=None, cwd=str(tmp_path))
    runner._client = HangingSDK()  # type: ignore[assignment]
    runner._effort = "high"
    restarting = asyncio.Event()

    async def restart() -> None:
        restarting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_reconnect", restart)
    sending = asyncio.create_task(runner.send("look", [SHOT], block_id="req-1"))
    await asyncio.wait_for(restarting.wait(), timeout=2)

    await cancelled(sending)

    assert files_left() == []
    assert runner.busy is False
    assert published.events("turn_started") == [], "no turn began, so none is ended"
    assert [event["text"] for event in published.events("notice")][-1] == UNREAD
    runner.channel.registry.close()


class IdleChild:
    """Stands in for Grok's own child process, which a stopped send never reaches."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def request(self, method: str, params: Any = None, timeout: Any = None) -> Any:
        self.requests.append(method)
        return {}

    async def close(self) -> None:
        return None


async def test_a_grok_send_stopped_while_its_turn_opens_takes_its_files(tmp_path: Path) -> None:
    published = Published()
    runner = GrokRunner(
        channel(tmp_path, "grok", published),
        binary="/bin/false",
        cwd=str(tmp_path),
        catalog=grok_catalog.load(),
    )
    child = IdleChild()
    runner._child = child  # type: ignore[assignment]
    runner._session_id = "grok-1"
    published.hold = "running"
    sending = asyncio.create_task(runner.send("look", [SHOT], block_id="req-1"))
    await asyncio.wait_for(published.held.wait(), timeout=2)
    published.hold = None

    await cancelled(sending)

    assert files_left() == []
    assert child.requests == [], "Grok was never asked anything"
    assert runner.busy is False
    assert [event["stop_reason"] for event in published.events("turn_completed")] == ["interrupted"]
    assert [event["text"] for event in published.events("notice")] == [GROK_UNREAD]
    await runner.close()
    runner.channel.registry.close()
