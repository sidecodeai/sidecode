"""A message sent into a running Codex turn keeps its files (A14, A49).

`turn/steer` takes the inputs `turn/start` takes, so a steered message carries
its images as `localImage` inputs and its other files named in the prompt, on
the shared daemon and on a private app-server alike. Its bubble, published when
Codex reads it, shows the words that were typed and names the files; a steer
Codex refuses is queued with its files instead, and `queue` and `interrupt`
deliver them as before.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client import attachments
from rc_client.agents.codex.adapter import CodexRunner
from rc_client.agents.codex.models import parse_catalog
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.fake_codex_daemon import FakeDaemon
from tests.test_codex_daemon_sessions import THREAD, Harness, settle, started
from tests.uploads import shown, upload

FIRST = "9a2f4c71-3e85-4d0b-b6a1-5c8e7d240f33"
STEERED = "0d6b8e29-4a17-4c3f-9b52-8e1a6f70d3c4"


def message_folders(session_id: str) -> list[Path]:
    """The folders the device wrote this session's attachments into."""
    folder = attachments.state_root() / session_id
    return sorted(folder.iterdir()) if folder.is_dir() else []


# ------------------------------------------------------------- shared daemon


@pytest.fixture
async def daemon(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    server = FakeDaemon(socket_dir / "codex.sock")
    await server.start()
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(server.path))
    built = Harness(tmp_path, server)
    try:
        yield built
    finally:
        await built.service.stop()
        await server.stop()
        built.close()


async def running(harness: Harness) -> None:
    """A turn this device started on the daemon, still working."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": FIRST, "session_id": THREAD, "text": "first"})
    await harness.daemon.notify(
        "turn/started", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "inProgress"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "running")


async def turn_completed(harness: Harness, status: str = "completed") -> None:
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": status}}
    )


async def test_a_steer_carries_images_as_inputs_and_names_other_files(daemon: Harness) -> None:
    await running(daemon)
    image, notes = upload(), upload("notes.txt", "text/plain")
    result = await daemon.hub.send(
        {
            "id": STEERED,
            "session_id": THREAD,
            "text": "also these",
            "attachments": [image, notes],
        }
    )
    assert result == {"accepted": "steered"}
    inputs = daemon.daemon.sent("turn/steer")[-1]["input"]
    assert [item["type"] for item in inputs] == ["text", "localImage"]
    assert inputs[0]["text"].startswith("also these\n\nAttached files:\n- notes.txt: /")
    written = Path(inputs[1]["path"])
    assert (written.name, written.read_bytes()) == (
        image["name"],
        base64.b64decode(image["data_base64"]),
    )

    # A14: the bubble lands when Codex echoes the prompt it was given, and it
    # shows what was typed and the files by name, never their paths.
    await daemon.daemon.echo_prompt(THREAD, inputs[0]["text"], item_id="u-steer")
    await settle(lambda: len(daemon.events("user_message")) == 2)
    bubble = daemon.events("user_message")[-1]
    assert (bubble["block_id"], bubble["text"]) == (STEERED, "also these")
    assert bubble["attachments"] == [shown(image), shown(notes)]


async def test_a_steer_the_turn_never_read_still_names_its_files(daemon: Harness) -> None:
    await running(daemon)
    image = upload()
    await daemon.hub.send(
        {"id": STEERED, "session_id": THREAD, "text": "also this", "attachments": [image]}
    )
    await turn_completed(daemon)
    await settle(lambda: len(daemon.events("user_message")) == 2)
    bubble = daemon.events("user_message")[-1]
    assert (bubble["block_id"], bubble["text"]) == (STEERED, "also this")
    assert bubble["attachments"] == [shown(image)]


async def test_a_steer_codex_refuses_is_queued_with_its_files(daemon: Harness) -> None:
    await running(daemon)
    daemon.daemon.errors["turn/steer"] = "expected turn mismatch"
    result = await daemon.hub.send(
        {"id": STEERED, "session_id": THREAD, "text": "also this", "attachments": [upload()]}
    )
    assert result == {"accepted": "queued", "queued_id": STEERED}
    # The files written for the refused steer were taken back (A49).
    assert message_folders(THREAD) == []

    await turn_completed(daemon)
    await settle(lambda: len(daemon.daemon.sent("turn/start")) == 2)
    inputs = daemon.daemon.sent("turn/start")[-1]["input"]
    assert [item["type"] for item in inputs] == ["text", "localImage"]


async def test_queue_holds_the_files_until_the_turn_ends(daemon: Harness) -> None:
    await running(daemon)
    result = await daemon.hub.send(
        {
            "id": STEERED,
            "session_id": THREAD,
            "text": "then this",
            "attachments": [upload()],
            "mode": "queue",
        }
    )
    assert result == {"accepted": "queued", "queued_id": STEERED}
    assert daemon.daemon.sent("turn/steer") == []
    await turn_completed(daemon)
    await settle(lambda: len(daemon.daemon.sent("turn/start")) == 2)
    inputs = daemon.daemon.sent("turn/start")[-1]["input"]
    assert [item["type"] for item in inputs] == ["text", "localImage"]


async def test_interrupt_stops_the_turn_and_sends_the_files(daemon: Harness) -> None:
    await running(daemon)
    sending = asyncio.create_task(
        daemon.hub.send(
            {
                "id": STEERED,
                "session_id": THREAD,
                "text": "stop and look",
                "attachments": [upload()],
                "mode": "interrupt",
            }
        )
    )
    await daemon.daemon.wait_for_call("turn/interrupt")
    await turn_completed(daemon, "interrupted")
    assert await sending == {"accepted": "sent"}
    inputs = daemon.daemon.sent("turn/start")[-1]["input"]
    assert [item["type"] for item in inputs] == ["text", "localImage"]


# ------------------------------------------------------- private app-server


class AppServer:
    """A private app-server that records every request and may refuse a steer."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.refuse_steer = False

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "turn/steer" and self.refuse_steer:
            raise RcError("internal", "no turn to steer")
        return {"turn": {"id": "turn-1"}}

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.requests if name == method]


@pytest.fixture
async def private(tmp_path: Path) -> AsyncIterator[tuple[CodexRunner, AppServer, list[Any]]]:
    """A `CodexRunner` in the middle of a turn, on a stand-in app-server."""
    registry = Registry(tmp_path / "codex.sqlite3")
    session = Session(session_id="s3", device_id="d", agent="codex", cwd=str(tmp_path))
    registry.upsert_session(session)
    frames: list[Any] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    runner = CodexRunner(
        SessionChannel(registry, session, publish),
        binary="/bin/codex",
        cwd=str(tmp_path),
        catalog=parse_catalog([]),
    )
    server = AppServer()
    runner._server = server  # type: ignore[assignment]
    runner._thread_id = "t1"
    runner._turn_id = "turn-1"
    try:
        yield runner, server, frames
    finally:
        registry.close()


def bubbles(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]


async def test_a_private_steer_names_its_files_and_its_bubble_does_not(
    private: tuple[CodexRunner, AppServer, list[Any]],
) -> None:
    runner, server, frames = private
    image = upload()
    assert await runner.steer_with_attachments("also this", [image], STEERED) is True
    [steered] = server.sent("turn/steer")
    prompt = steered["input"][0]["text"]
    assert prompt.startswith(f"also this\n\nAttached files:\n- {image['name']}: /")
    assert bubbles(frames) == []

    echo = {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": prompt}]}
    await runner._on_notification("item/started", {"item": echo})
    [bubble] = bubbles(frames)
    assert (bubble["block_id"], bubble["text"]) == (STEERED, "also this")
    assert bubble["attachments"] == [shown(image)]


async def test_a_private_steer_codex_refuses_takes_its_files_back(
    private: tuple[CodexRunner, AppServer, list[Any]],
) -> None:
    runner, server, _ = private
    server.refuse_steer = True
    assert await runner.steer_with_attachments("also this", [upload()], STEERED) is False
    assert len(server.sent("turn/steer")) == 1
    assert message_folders("s3") == []
