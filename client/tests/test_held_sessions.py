"""Amendment A58: an app's message holds the session until the person closes it.

A message to a session nothing holds makes it the device's: `remote` as the turn
starts and after it ends, across restarts and updates, until Close. Its Claude or
pi process stops after 30 minutes unused and the row stays; a terminal that takes
it hands it back to the device when it leaves.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude.holders import Holder, HolderScan
from rc_client.agents.codex.daemon import threads
from rc_client.agents.grok import control as grok_control
from rc_client.agents.pi import frames
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions import idle
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.attach import Attachment
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_codex_daemon_sessions import THREAD, Harness, device_driven, harness
from tests.test_hub import FakeRunner, agent_info
from tests.test_pi_extension import Bench, bench, connect, settle

__all__ = ["bench", "harness"]


class Built:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "state.sqlite3"
        self.registry = Registry(self.path)
        self.frames: list[dict[str, Any]] = []
        self.runners: list[FakeRunner] = []

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", agent_info)

        async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
            runner = FakeRunner(entry.channel)
            self.runners.append(runner)
            return runner

        self.hub._build_runner = build  # type: ignore[method-assign]

    def seed(self, session_id: str = "s-1", **fields: Any) -> SessionEntry:
        session = Session(
            session_id=session_id,
            device_id="dev-1",
            agent=str(fields.pop("agent", "claude")),
            cwd="/repo",
            state=fields.pop("state", "idle"),
            origin=fields.pop("origin", "terminal"),
            control=fields.pop("control", "none"),
            **fields,
        )
        entry = self.hub.register_mirrored(session)
        return entry

    def controls(self, session_id: str = "s-1") -> list[str]:
        return [
            str(frame["event"]["control"])
            for frame in self.frames
            if frame.get("type") == "session.event"
            and frame["session_id"] == session_id
            and frame["event"]["kind"] == "meta"
            and "control" in frame["event"]
        ]


@pytest.fixture
def built(tmp_path: Path) -> Any:
    made = Built(tmp_path)
    yield made
    made.registry.close()


async def test_a_message_to_an_archived_session_holds_it(built: Built) -> None:
    entry = built.seed(archived=True, state="stopped")
    await built.hub.send({"id": "m-1", "session_id": "s-1", "text": "carry on"})

    session = entry.session
    assert (session.held, session.archived, session.control) == (True, False, "remote")
    assert built.controls() == ["remote"]
    # The turn ends; the device keeps the session.
    await built.runners[0].finish()
    assert session.control == "remote"
    # What the registry keeps says so; the wire never does.
    stored = {s.session_id: s for s in built.registry.load_sessions()}["s-1"]
    assert stored.held is True
    assert "held" not in session.to_dict()


async def test_a_restart_brings_held_and_launched_sessions_back_remote(
    built: Built, tmp_path: Path
) -> None:
    held = built.seed("held-1")
    await built.hub.send({"id": "m-1", "session_id": "held-1", "text": "go"})
    await built.runners[0].finish()
    built.seed("launched-1", origin="remote", control="remote")
    built.seed("closed-1", origin="remote", control="none", archived=True, state="stopped")
    built.seed("nobody-1")
    assert held.session.held is True
    built.registry.close()

    again = Built(tmp_path)
    try:
        again.hub.load()
        controls = {sid: e.session.control for sid, e in again.hub.entries.items()}
        assert controls == {
            "held-1": "remote",
            "launched-1": "remote",
            "closed-1": "none",
            "nobody-1": "none",
        }
        restored = again.hub.entry("held-1")
        assert (restored.session.state, restored.runner) == ("idle", None)
        # The next message starts its process again.
        await again.hub.send({"id": "m-2", "session_id": "held-1", "text": "and again"})
        assert again.runners and again.runners[0].sent == ["and again"]
    finally:
        again.registry.close()
    built.registry = Registry(built.path)


async def test_close_lets_go_of_a_held_session_with_no_process(built: Built) -> None:
    entry = built.seed("s-1", held=True, control="remote")
    result = await built.hub.archive({"session_id": "s-1", "archived": True})
    session = result["session"]
    assert (session["archived"], session["control"], session["state"]) == (True, "none", "stopped")
    assert entry.session.held is False
    stored = {s.session_id: s for s in built.registry.load_sessions()}["s-1"]
    assert stored.held is False


async def test_an_idle_process_is_stopped_after_thirty_minutes_and_the_row_stays(
    built: Built,
) -> None:
    entry = built.seed()
    await built.hub.send({"id": "m-1", "session_id": "s-1", "text": "go"})
    runner = built.runners[0]
    later = time.monotonic() + idle.IDLE_RELEASE + 1

    # A turn running is no reason to stop, however long ago the session was used.
    assert await built.hub.idle.sweep(later) == 0
    await runner.finish()
    assert await built.hub.idle.sweep(time.monotonic()) == 0, "used a moment ago"
    assert await built.hub.idle.sweep(later) == 1

    assert runner.closed is True
    assert entry.runner is None
    assert (entry.session.control, entry.session.state) == ("remote", "idle")
    # The next message starts the process again.
    await built.hub.send({"id": "m-2", "session_id": "s-1", "text": "back again"})
    assert built.runners[1].sent == ["back again"]


async def test_queued_messages_keep_a_process(built: Built) -> None:
    entry = built.seed()
    await built.hub.send({"id": "m-1", "session_id": "s-1", "text": "go"})
    await built.hub.send({"id": "m-2", "session_id": "s-1", "text": "next", "mode": "queue"})
    entry.runner._busy = False  # type: ignore[union-attr]
    entry.session.turn = None
    entry.session.state = "idle"
    assert await built.hub.idle.sweep(time.monotonic() + idle.IDLE_RELEASE + 1) == 0


async def test_a_session_nobody_holds_is_never_released(built: Built) -> None:
    entry = built.seed()
    entry.runner = FakeRunner(entry.channel)
    assert await built.hub.idle.sweep(time.monotonic() + idle.IDLE_RELEASE + 1) == 0


class Bridge(Attachment):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id=session_id, cwd="/repo", pid=0, claude_version="2.1.300")

    async def inject(self, message_id: str, text: str) -> bool:
        return True

    def detach(self) -> None:
        return None


async def test_a_terminal_that_takes_a_held_session_hands_it_back(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rc_client.agents.claude import transcripts

    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    entry = built.seed()
    await built.hub.send({"id": "m-1", "session_id": "s-1", "text": "go"})
    await built.runners[0].finish()

    bridge = Bridge("s-1")
    await built.hub.attach_registered(bridge)
    # The device's idle process gave way: one writer.
    assert built.runners[0].closed is True
    assert entry.runner is None
    assert entry.session.to_dict()["control"] == "shared"

    await built.hub.attach_closed(bridge)
    assert entry.session.to_dict()["control"] == "remote"
    assert entry.session.to_dict()["state"] == "idle"


async def test_a_plain_terminal_resuming_a_held_session_stops_the_device_s_process(
    built: Built, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = built.seed()
    await built.hub.send({"id": "m-1", "session_id": "s-1", "text": "go"})
    await built.runners[0].finish()
    cli = Holder(pid=900, identity=(900, "s"), session_id="s-1", cwd="/repo")
    holders = [cli]

    async def scan(*args: Any, **kwargs: Any) -> HolderScan:
        return HolderScan(holders=list(holders), complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", scan)
    mirror = MirrorService(built.hub)
    await mirror._refresh_claude_control()
    assert built.runners[0].closed is True
    assert (entry.session.control, entry.holder_pid) == ("terminal", 900)

    holders.clear()
    await mirror._refresh_claude_control()
    assert entry.session.control == "remote"


def test_the_codex_table_holds_what_the_device_holds() -> None:
    def resolve(**situation: bool) -> tuple[str, str]:
        base = {"created_here": False, "loaded": True, "terminal_holds": False}
        return threads.resolve(**{**base, **situation})

    assert resolve() == ("terminal", "none")
    assert resolve(held=True) == ("terminal", "remote")
    assert resolve(held=True, loaded=False) == ("terminal", "remote")
    assert resolve(held=True, terminal_holds=True) == ("terminal", "shared")
    assert resolve(created_here=True, loaded=False) == ("remote", "remote")
    assert resolve(created_here=True, closed=True) == ("remote", "none")
    assert resolve(held=True, closed=True) == ("terminal", "none")


def test_the_grok_table_holds_what_the_device_holds() -> None:
    def resolve(**situation: bool) -> tuple[str, str]:
        base = {"created_here": False, "registered": False, "attached": True}
        return grok_control.resolve(**{**base, **situation})

    assert resolve() == ("terminal", "none")
    assert resolve(held=True) == ("terminal", "remote")
    assert resolve(held=True, attached=False) == ("terminal", "remote")
    assert resolve(held=True, registered=True) == ("terminal", "shared")
    assert resolve(created_here=True, attached=False) == ("remote", "remote")
    assert resolve(created_here=True, closed=True) == ("remote", "none")


# ------------------------------------------------------- daemon, leader, pi


async def test_a_codex_thread_a_message_took_stays_remote_after_its_turn(
    harness: Harness,
) -> None:
    await device_driven(harness)
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    entry = harness.hub.entry(THREAD)
    assert (entry.session.held, entry.session.control) == (True, "remote")
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.1)
    assert entry.session.turn is None
    assert entry.session.control == "remote"


async def test_a_pi_that_leaves_a_held_session_hands_it_back(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello()
    await settle()
    bench.entry().session.held = True
    await extension.send({"type": frames.BYE, "reason": "quit"})
    extension.close()
    await settle()
    assert bench.entry().session.control == "remote"
    assert bench.entry().runner is None
