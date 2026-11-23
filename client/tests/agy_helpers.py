"""What the Antigravity tests share: the fake `agy`, a session to drive it in, and waits."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rc_client.agents.antigravity.opening import Opening
from rc_client.agents.antigravity.options import EFFORTS, FALLBACK_MODES
from rc_client.agents.antigravity.plugin import CAPABILITIES
from rc_client.agents.antigravity.runner import AntigravityRunner
from rc_client.agents.antigravity.service import AntigravityService
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry

FAKE = Path(__file__).resolve().parent / "fake_agy.py"
SESSION = "d055434c-60ae-474a-b411-363f558a64c0"


class Recorder:
    """Every frame a session published."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def kinds(self) -> list[str]:
        return [
            frame["event"]["kind"] for frame in self.frames if frame.get("type") == "session.event"
        ]

    def states(self) -> list[str]:
        return [event["state"] for event in self.events("status")]


class Peer:
    """The fake's scratch directory: what it was told to be, and what it was asked."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def configure(self, **config: Any) -> None:
        path = self.directory / "config.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        path.write_text(json.dumps({**current, **config}))

    def requests(self) -> list[dict[str, Any]]:
        path = self.directory / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [
            dict(message.get("params") or {})
            for message in self.requests()
            if message.get("method") == method
        ]

    def methods(self) -> list[str]:
        return [str(message["method"]) for message in self.requests() if message.get("method")]

    def replies(self) -> list[dict[str, Any]]:
        """The client's answers to the requests the fake made."""
        return [message for message in self.requests() if "method" not in message]

    def starts(self) -> list[dict[str, Any]]:
        path = self.directory / "starts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]


def fake_agy(tmp_path: Path, **config: Any) -> tuple[str, Peer]:
    """An executable named `agy` that runs the fake, in a directory of the test's own."""
    directory = tmp_path / "agy-peer"
    directory.mkdir(parents=True, exist_ok=True)
    peer = Peer(directory)
    peer.configure(**config)
    folder = tmp_path / "agy-bin"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "agy"
    path.write_text(
        f'#!/bin/sh\nRC_FAKE_AGY_DIR="{directory}" exec "{sys.executable}" "{FAKE}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path), peer


def entry_for(
    tmp_path: Path,
    recorder: Recorder,
    *,
    session_id: str = "temp-1",
    cwd: str | None = None,
    **fields: Any,
) -> SessionEntry:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(
        session_id=session_id,
        device_id="d1",
        agent="antigravity",
        cwd=cwd or str(tmp_path),
        **fields,
    )
    registry.upsert_session(session)
    return SessionEntry(session=session, channel=SessionChannel(registry, session, recorder))


def runner_for(
    entry: SessionEntry,
    binary: str,
    *,
    opening: Opening | None = None,
    service: AntigravityService | None = None,
    turn_ends: list[str] | None = None,
) -> AntigravityRunner:
    async def on_session_id(session_id: str) -> None:
        entry.session.session_id = session_id

    async def on_turn_end() -> None:
        if turn_ends is not None:
            turn_ends.append("ended")

    return AntigravityRunner(
        entry,
        binary=binary,
        opening=opening or Opening("new"),
        service=service or AntigravityService(),
        on_turn_end=on_turn_end,
        on_session_id=on_session_id,
    )


def agent_info(binary: str | None, version: str = "1.5.14") -> AgentInfo:
    return AgentInfo(
        agent="antigravity",
        available=binary is not None,
        version=version,
        path=binary,
        permission_modes=list(FALLBACK_MODES),
        efforts=list(EFFORTS),
        capabilities=list(CAPABILITIES),
    )


async def wait_for(check: Callable[[], object], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for the Antigravity session")
