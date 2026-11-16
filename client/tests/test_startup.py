"""A restarted device is online within seconds, whatever its agents and its history cost.

The link used to come up only once the device had read every event it had stored, reached the
shared Codex daemon, withdrawn the `codex exec` runs published before A59, listed Codex's
threads, joined the Grok leader and asked every agent what it offers: minutes, on a loaded
machine with thousands of such runs. Now the link waits for none of it. The agents start in the
background and the requests that act through one wait for them; the withdrawals go a batch at a
time; a rollout's first record is read once; and a session's block positions are read when it
first writes a block.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client import config as config_module
from rc_client import daemon as daemon_module
from rc_client.agents import announced
from rc_client.agents.codex import heads as heads_module
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.codex.daemon.service import PRUNE_BATCH, CodexDaemonService
from rc_client.agents.grok import sessions as grok_sessions
from rc_client.agents.grok.service import GrokLeaderService
from rc_client.daemon import Daemon
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_codex_daemon_sessions import RecordingRepair
from tests.test_codex_exec_runs import published, write_rollout
from tests.test_first_hello import device_config
from tests.test_gateway_link import FakeGateway

LAST_RUN = [AgentInfo(agent="claude", available=True, version="1.0").to_dict()]
THIS_RUN = [AgentInfo(agent="claude", available=True, version="2.0")]


@contextlib.asynccontextmanager
async def starting_daemon(
    monkeypatch: pytest.MonkeyPatch, agents_started: asyncio.Event
) -> AsyncIterator[tuple[Daemon, FakeGateway]]:
    """A daemon whose Codex daemon and Grok leader answer only once `agents_started` is set."""
    registry = Registry(config_module.database_path())
    announced.save(registry, LAST_RUN)
    registry.close()
    server = FakeGateway()
    await server.start()
    daemon = Daemon(device_config())
    daemon.link.url = server.url

    async def reach(binary: str | None = None) -> bool:
        await agents_started.wait()
        return False

    async def detect(context: Any = None) -> list[AgentInfo]:
        return THIS_RUN

    monkeypatch.setattr(daemon.codex, "start", reach)
    monkeypatch.setattr(daemon.grok, "ensure", reach)
    monkeypatch.setattr(daemon_module, "detect_all", detect)
    task = asyncio.create_task(daemon.run())
    try:
        yield daemon, server
    finally:
        agents_started.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await server.stop()


async def reply_to(server: FakeGateway, identifier: str, timeout: float) -> dict[str, Any]:
    async def poll() -> dict[str, Any]:
        while True:
            for reply in server.frames("reply"):
                if reply.get("id") == identifier:
                    return reply
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=timeout)


async def test_the_link_comes_up_while_the_agents_are_still_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_started = asyncio.Event()
    async with starting_daemon(monkeypatch, agents_started) as (daemon, server):
        hello = await server.wait_for("hello", timeout=5)
        # What the last run detected, rather than a wait for this one's.
        assert hello["agents"] == LAST_RUN
        assert server.frames("agents.updated") == []

        agents_started.set()
        updated = await server.wait_for("agents.updated", timeout=5)
        assert updated["agents"] == [info.to_dict() for info in THIS_RUN]
        # And it is what the next run's first hello will say.
        assert announced.load(daemon.registry) == updated["agents"]


async def test_a_request_that_acts_through_an_agent_waits_for_the_agents_and_a_read_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_started = asyncio.Event()
    async with starting_daemon(monkeypatch, agents_started) as (_, server):
        await server.wait_for("hello", timeout=5)
        await server.send({"type": "session.history", "id": "read", "session_id": "gone"})
        await server.send({"type": "session.commands", "id": "act", "session_id": "gone"})
        await reply_to(server, "read", timeout=5)
        await asyncio.sleep(0.3)
        assert [reply["id"] for reply in server.frames("reply")] == ["read"]

        agents_started.set()
        await reply_to(server, "act", timeout=5)


async def test_the_wait_for_the_agents_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_module, "AGENT_START_WAIT", 0.2)
    agents_started = asyncio.Event()
    async with starting_daemon(monkeypatch, agents_started) as (_, server):
        await server.wait_for("hello", timeout=5)
        await server.send({"type": "session.commands", "id": "act", "session_id": "gone"})
        await reply_to(server, "act", timeout=5)
        assert not agents_started.is_set()


async def test_thousands_of_exec_runs_are_withdrawn_a_batch_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "codex-sessions"
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", root)
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    hub.link_up = lambda: False
    runs = [f"t-{index:04d}" for index in range(3 * PRUNE_BATCH + 5)]
    for thread_id in runs:
        write_rollout(root, thread_id, "codex_exec", "exec")
        published(hub, thread_id)
    registry.flush()
    batches: list[int] = []
    read = codex_rollouts.owned_at

    def owned_at(paths: dict[str, list[str]]) -> dict[str, bool]:
        # The batch before has been written: nothing of it waits in the registry's queue.
        assert registry._queued == []
        batches.append(len(paths))
        return read(paths)

    monkeypatch.setattr(codex_rollouts, "owned_at", owned_at)
    turns = 0

    async def other_work() -> None:
        nonlocal turns
        while True:
            turns += 1
            await asyncio.sleep(0)

    worker = asyncio.create_task(other_work())
    try:
        await CodexDaemonService(hub, "0.1.0").prune_foreign()
    finally:
        worker.cancel()
    try:
        assert hub.entries == {}
        assert sum(batches) == len(runs)
        assert len(batches) == 4 and max(batches) == PRUNE_BATCH
        # The loop went on with other work all along.
        assert turns > len(runs)
        # Every removal is said again on the next link, not only the last 64.
        frames.clear()
        hub.link_up = lambda: True
        hub.snapshot()
        await hub.sweep_ghosts()
        removed = [frame["session_id"] for frame in frames if frame["type"] == "session.removed"]
        assert sorted(removed) == runs
    finally:
        await hub.close()
        registry.close()


def test_a_rollout_s_first_record_is_read_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    for index in range(30):
        write_rollout(tmp_path, f"t-exec-{index}", "codex_exec", "exec")
    write_rollout(tmp_path, "t-tui", "codex-tui", "cli")
    reads: list[str] = []
    real = heads_module.read_head

    def read_head(path: str) -> heads_module.Head | None:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(heads_module, "read_head", read_head)

    def found() -> set[str]:
        return {info.thread_id for info in codex_rollouts.discover()}

    assert found() == {"t-tui"}
    assert len(reads) == 31
    assert found() == {"t-tui"}
    assert len(reads) == 31

    # One that is still being written is read again until it can be.
    partial = write_rollout(tmp_path, "t-new", "codex-tui", "cli")
    whole = partial.read_text(encoding="utf-8")
    partial.write_text(whole[:20], encoding="utf-8")
    assert found() == {"t-tui"}
    partial.write_text(whole, encoding="utf-8")
    assert found() == {"t-tui", "t-new"}
    assert len(reads) == 33
    assert found() == {"t-tui", "t-new"}
    assert len(reads) == 33


async def test_loading_reads_no_event_and_a_block_keeps_where_it_first_appeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def publish(frame: dict[str, Any]) -> None:
        return None

    path = tmp_path / "state.sqlite3"
    registry = Registry(path)
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    entry = hub.register_mirrored(
        Session(
            session_id="s-1",
            device_id="dev-1",
            agent="claude",
            cwd="/repo",
            state="idle",
            origin="terminal",
            control="none",
        )
    )
    first = await entry.channel.emit("tool_call", block_id="b1", tool="Bash", status="running")
    await entry.channel.emit("assistant_text", block_id="b2", text="working on it")
    await hub.close()
    registry.close()

    registry = Registry(path)
    asked: list[str] = []
    seqs = registry.first_seqs

    def first_seqs(session_id: str) -> dict[str, int]:
        asked.append(session_id)
        return seqs(session_id)

    monkeypatch.setattr(registry, "first_seqs", first_seqs)
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    try:
        hub.load()
        assert asked == []
        done = await hub.entry("s-1").channel.emit(
            "tool_call", block_id="b1", tool="Bash", status="succeeded"
        )
        # A8: the block stays where it first appeared, across the restart.
        assert done["first_seq"] == first["first_seq"] < done["seq"]
        assert asked == ["s-1"]
    finally:
        await hub.close()
        registry.close()


async def test_the_mirror_reads_codex_and_grok_files_once_their_services_were_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread the shared daemon or the leader serves is theirs to describe (A11, A28)."""
    asked: list[str] = []

    def discovered(agent: str) -> Any:
        def discover(*args: Any) -> list[Any]:
            asked.append(agent)
            return []

        return discover

    monkeypatch.setattr(codex_rollouts, "discover", discovered("codex"))
    monkeypatch.setattr(grok_sessions, "discover", discovered("grok"))

    async def publish(frame: dict[str, Any]) -> None:
        return None

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    codex = CodexDaemonService(hub, "0.1.0")
    grok = GrokLeaderService(hub)
    mirror = MirrorService(hub, codex_daemon=codex, grok_leader=grok)
    try:
        await mirror.scan_once(settings=False)
        assert asked == []
        # Neither is there to reach on this machine, and trying is what counts.
        assert await codex.start(None) is False
        assert await grok.ensure() is False
        await mirror.scan_once(settings=False)
        assert asked == ["codex", "grok"]
    finally:
        await hub.close()
        registry.close()


async def test_a_tick_follows_an_attempt_under_way_instead_of_making_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def publish(frame: dict[str, Any]) -> None:
        return None

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    service = CodexDaemonService(hub, "0.1.0", repair=RecordingRepair())
    answer = asyncio.Event()
    attempts = 0

    async def connect(binary: str | None) -> bool:
        nonlocal attempts
        attempts += 1
        await answer.wait()
        return False

    monkeypatch.setattr(service, "_connect", connect)
    try:
        first = asyncio.create_task(service.start(None))
        await asyncio.sleep(0)
        tick = asyncio.create_task(service.tick(None))
        await asyncio.sleep(0.05)
        assert (attempts, service.attempted) == (1, False)
        answer.set()
        assert await first is False
        await tick
        # The tick waited for that answer, then tried again on its own round, as it always has.
        assert (attempts, service.attempted) == (2, True)
    finally:
        await service.stop()
        await hub.close()
        registry.close()
