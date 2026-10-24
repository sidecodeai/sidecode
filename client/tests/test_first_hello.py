"""A restarted daemon's first hello says which terminals still hold their sessions.

The hub restores every persisted session as held by nothing, and the gateway shows no unseen mark
(A47) for a session nothing holds, so a hello that said so would hide the mark of every terminal
session still running when the daemon restarted, an update's restart included, until a scan found
it. The mirror's first scan therefore runs while the link connects, and the first hello waits for
it, up to `FIRST_SCAN_WAIT`. The gateway's `test_unseen.py` holds the other half: a hello that
reports a terminal shows the mark, one that reports none hides it, and the session keeps it either
way for when something holds it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from websockets.asyncio.server import ServerConnection, serve

from rc_client import config as config_module
from rc_client import daemon as daemon_module
from rc_client.agents.claude import transcripts as claude_transcripts
from rc_client.agents.claude.holders import Holder, HolderScan
from rc_client.agents.claude.transcripts import TranscriptInfo
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.codex.daemon.service import CodexDaemonService
from rc_client.config import Config
from rc_client.daemon import Daemon
from rc_client.gateway import GatewayLink
from rc_client.models import Session
from rc_client.proxy import DIRECT
from rc_client.registry import Registry
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions import offsets
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_gateway_link import FakeGateway
from tests.test_mirroring import seed_transcript


def device_config() -> Config:
    return Config(
        gateway_origin="http://127.0.0.1:8787",
        device_id="dev-1",
        device_token="tok-1",
        name="test-device",
    )


def previous_run(tmp_path: Path) -> list[TranscriptInfo]:
    """The device as its last run left it: two Claude terminal sessions, each held by a CLI."""
    registry = Registry(config_module.database_path())
    root = tmp_path / "projects"
    root.mkdir()
    found: list[TranscriptInfo] = []
    for name in ("alive", "gone"):
        registry.upsert_session(
            Session(
                session_id=name,
                device_id="dev-1",
                agent="claude",
                cwd=str(tmp_path),
                state="readonly",
                origin="terminal",
                control="terminal",
            )
        )
        info = seed_transcript(root, name, str(tmp_path))
        # Mirrored before; only what is appended is new.
        registry.set_kv(offsets.offset_key(name), str(info.size))
        found.append(info)
    registry.close()
    return found


def find_terminals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    found: list[TranscriptInfo],
    gate: asyncio.Event | None = None,
) -> None:
    """Point the scan at those transcripts and at one CLI, still running `alive`.

    The process list is what takes time on a real machine, so `gate` holds it back.
    """
    agent_state = tmp_path / "agent-state"
    agent_state.mkdir()
    monkeypatch.setattr(claude_transcripts, "PROJECTS_DIR", agent_state / "claude")
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", agent_state / "codex")
    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: found)
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])

    async def scan() -> HolderScan:
        if gate is not None:
            await gate.wait()
        cli = Holder(pid=100, identity=(100, "a"), session_id="alive", cwd=str(tmp_path))
        return HolderScan(holders=[cli], complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", scan)


@contextlib.asynccontextmanager
async def restarted_daemon() -> AsyncIterator[tuple[Daemon, FakeGateway]]:
    server = FakeGateway()
    await server.start()
    daemon = Daemon(device_config())
    daemon.link.url = server.url
    task = asyncio.create_task(daemon.run())
    try:
        yield daemon, server
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await server.stop()


def announced(hello: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {item["session_id"]: (item["control"], item["state"]) for item in hello["sessions"]}


UNSETTLED = "first hello sent before the first terminal scan settled"


def warnings_about_the_first_hello(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage() == UNSETTLED]


async def test_a_live_terminal_session_is_announced_as_held_and_an_exited_one_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    find_terminals(monkeypatch, tmp_path, previous_run(tmp_path))
    async with restarted_daemon() as (_, server):
        hello = await server.wait_for("hello", timeout=10)
    # The CLI of `alive` is still there, so the gateway shows its mark; the one of `gone` died
    # while the daemon was down, so the gateway shows none.
    assert announced(hello) == {"alive": ("terminal", "readonly"), "gone": ("none", "idle")}
    assert warnings_about_the_first_hello(caplog) == []


async def test_a_scan_still_running_does_not_hold_the_hello_back_for_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gate = asyncio.Event()
    find_terminals(monkeypatch, tmp_path, previous_run(tmp_path), gate)
    monkeypatch.setattr(daemon_module, "FIRST_SCAN_WAIT", 0.2)
    async with restarted_daemon() as (_, server):
        hello = await server.wait_for("hello", timeout=10)
        # Nothing has been decided yet, so both are announced as the hub restored them, and the
        # log says the hello went out early.
        assert announced(hello) == {"alive": ("none", "idle"), "gone": ("none", "idle")}
        assert warnings_about_the_first_hello(caplog) == [UNSETTLED]

        gate.set()

        async def updated() -> dict[str, Any]:
            while True:
                for frame in server.frames("session.updated"):
                    session: dict[str, Any] = frame["session"]
                    if session["session_id"] == "alive" and session["control"] == "terminal":
                        return session
                await asyncio.sleep(0.01)

        # What the scan finds next reaches the gateway as an ordinary update.
        assert (await asyncio.wait_for(updated(), timeout=10))["state"] == "readonly"


async def test_only_the_first_hello_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gate = asyncio.Event()
    find_terminals(monkeypatch, tmp_path, previous_run(tmp_path), gate)
    monkeypatch.setattr(daemon_module, "FIRST_SCAN_WAIT", 0.3)
    daemon = Daemon(device_config())
    daemon.hub.load()
    daemon.mirror.start()
    try:
        started = time.monotonic()
        await daemon._hello()
        first = time.monotonic() - started
        started = time.monotonic()
        await daemon._hello()
        second = time.monotonic() - started
    finally:
        gate.set()
        await daemon.mirror.stop()
        await daemon.hub.close()
        daemon.registry.close()
    assert first >= 0.25
    assert second < 0.1
    assert warnings_about_the_first_hello(caplog) == [UNSETTLED]


async def test_a_change_after_the_snapshot_waits_behind_the_hello_for_the_ack() -> None:
    """The scan that outlasts the wait must not lose what it finds between hello and ack."""
    received: list[str] = []
    hello_seen = asyncio.Event()
    ack_may_come = asyncio.Event()

    async def gateway(socket: ServerConnection) -> None:
        async for raw in socket:
            frame = json.loads(raw)
            received.append(frame["type"])
            if frame["type"] == "hello":
                hello_seen.set()
                await ack_may_come.wait()
                await socket.send(json.dumps({"type": "hello_ack"}))

    snapshot_due = asyncio.Event()
    connected = asyncio.Event()

    async def hello() -> dict[str, Any]:
        connected.set()
        await snapshot_due.wait()
        return {"name": "test-device", "agents": [], "sessions": []}

    server = await serve(gateway, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    link = GatewayLink(
        f"ws://127.0.0.1:{port}/ws/device", "tok", hello=hello, handlers={}, proxy=DIRECT
    )
    link.start()
    try:
        await asyncio.wait_for(connected.wait(), timeout=5)
        # Before the snapshot: it will hold what this describes.
        await link.send({"type": "before"})
        snapshot_due.set()
        await asyncio.wait_for(hello_seen.wait(), timeout=5)
        # After it, and before the ack: nothing else will say it.
        await link.send({"type": "after"})
        ack_may_come.set()

        async def arrived() -> None:
            while "after" not in received:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(arrived(), timeout=5)
    finally:
        await link.stop()
        server.close()
        await server.wait_closed()
    assert received == ["hello", "after"]


def bare_mirror(tmp_path: Path) -> tuple[MirrorService, Registry]:
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    return MirrorService(SessionHub(registry, publish, "dev-1", lambda: [])), registry


async def test_waiting_for_a_mirror_that_never_started_costs_nothing(tmp_path: Path) -> None:
    mirror, registry = bare_mirror(tmp_path)
    assert await asyncio.wait_for(mirror.wait_settled(30), timeout=1) is True
    registry.close()


async def test_a_first_scan_that_fails_still_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror, registry = bare_mirror(tmp_path)

    async def failing(*, settings: bool = True) -> None:
        raise OSError("the state directory is unreadable")

    monkeypatch.setattr(mirror, "scan_once", failing)
    mirror.start()
    try:
        assert await asyncio.wait_for(mirror.wait_settled(30), timeout=5) is True
    finally:
        await mirror.stop()
        registry.close()


class StuckCodexDaemon:
    """A shared Codex daemon whose scan tick is waiting on its app-server."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def tick(self, binary: str | None) -> None:
        await self.release.wait()

    def knows(self, thread_id: str) -> bool:
        return False


async def test_the_first_scan_decides_claude_holders_before_it_waits_on_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = previous_run(tmp_path)
    find_terminals(monkeypatch, tmp_path, found)
    registry = Registry(config_module.database_path())

    async def publish(frame: dict[str, Any]) -> None:
        return None

    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    hub.load()
    stuck = StuckCodexDaemon()
    mirror = MirrorService(hub, codex_daemon=cast(CodexDaemonService, stuck))
    scan = asyncio.create_task(mirror.scan_once(settings=False))
    try:

        async def held() -> None:
            while hub.entry("alive").session.control != "terminal":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(held(), timeout=5)
        assert hub.entry("gone").session.control == "none"
        # The Codex tick is still stuck: Claude's terminals did not wait for it.
        assert not scan.done()
    finally:
        stuck.release.set()
        await scan
        await hub.close()
        registry.close()
