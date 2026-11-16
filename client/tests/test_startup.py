"""A restarted device is online within seconds, whatever its agents and its history cost.

The link used to come up only once the device had read every event it had stored, reached the
shared Codex daemon, withdrawn the `codex exec` runs published before A59, listed Codex's
threads, joined the Grok leader and asked every agent what it offers: minutes, on a loaded
machine with thousands of such runs. Now the link waits for none of it. Each agent starts on its
own in the background, and a request that acts through one waits for that one alone, at most
`AGENT_START_WAIT`, and is refused and never run past it; the withdrawals go a batch at a time;
a rollout's first record is read once; and a session's block positions are read when it first
writes a block.
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
from rc_client.agents.registry import DetectContext
from rc_client.agents.startup import AgentStartup
from rc_client.daemon import Daemon
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_codex_daemon_sessions import RecordingRepair
from tests.test_codex_exec_runs import published, write_rollout
from tests.test_first_hello import device_config
from tests.test_gateway_link import FakeGateway

LAST_RUN = [
    AgentInfo(agent="claude", available=True, version="1.0").to_dict(),
    AgentInfo(agent="codex", available=True, version="1.0").to_dict(),
]
THIS_RUN = {
    "claude": AgentInfo(agent="claude", available=True, version="2.0"),
    "codex": AgentInfo(agent="codex", available=True, version="2.0"),
}


@contextlib.asynccontextmanager
async def starting_daemon(
    monkeypatch: pytest.MonkeyPatch, codex_attached: asyncio.Event
) -> AsyncIterator[tuple[Daemon, FakeGateway, list[str]]]:
    """A restarted daemon whose shared Codex daemon and Grok leader answer only once
    `codex_attached` is set, and the sessions `session.send` was carried out for."""
    registry = Registry(config_module.database_path())
    announced.save(registry, LAST_RUN)
    for session_id, agent in (("s-claude", "claude"), ("s-codex", "codex")):
        registry.upsert_session(
            Session(session_id=session_id, device_id="dev-1", agent=agent, cwd="/repo")
        )
    registry.close()
    sent: list[str] = []

    async def send(self: SessionHub, frame: dict[str, Any]) -> dict[str, Any]:
        sent.append(str(frame["session_id"]))
        return {"accepted": "sent"}

    # Before the daemon is built: it takes its handlers as it is built.
    monkeypatch.setattr(SessionHub, "send", send)
    server = FakeGateway()
    await server.start()
    daemon = Daemon(device_config())
    daemon.link.url = server.url

    async def reach(binary: str | None = None) -> bool:
        await codex_attached.wait()
        return False

    async def detect(agent: str, context: DetectContext) -> AgentInfo | None:
        return THIS_RUN.get(agent)

    monkeypatch.setattr(daemon.codex, "start", reach)
    monkeypatch.setattr(daemon.grok, "ensure", reach)
    monkeypatch.setattr(daemon_module, "detect_one", detect)
    task = asyncio.create_task(daemon.run())
    try:
        yield daemon, server, sent
    finally:
        codex_attached.set()
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


def send_frame(identifier: str, session_id: str) -> dict[str, Any]:
    return {"type": "session.send", "id": identifier, "session_id": session_id, "text": "hi"}


def told(server: FakeGateway, agent: str) -> dict[str, Any] | None:
    """What the gateway was last told about one agent, by the hello or an update."""
    for frame in reversed(server.frames("hello")[:1] + server.frames("agents.updated")):
        for item in frame["agents"]:
            if item["agent"] == agent:
                return dict(item)
        return None
    return None


async def test_the_link_comes_up_while_the_agents_are_still_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_attached = asyncio.Event()
    async with starting_daemon(monkeypatch, codex_attached) as (daemon, server, _):
        hello = await server.wait_for("hello", timeout=5)
        # Codex has not started: the hello says what the last run found rather than wait.
        assert [item for item in hello["agents"] if item["agent"] == "codex"] == LAST_RUN[1:]

        codex_attached.set()

        async def codex_said() -> None:
            while told(server, "codex") != THIS_RUN["codex"].to_dict():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(codex_said(), timeout=5)
        listed = [info.to_dict() for info in THIS_RUN.values()]
        assert server.frames("agents.updated")[-1]["agents"] == listed
        # And it is what the next run's first hello will say.
        assert announced.load(daemon.registry) == listed


async def test_a_claude_send_goes_through_while_codex_is_still_attaching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_attached = asyncio.Event()
    async with starting_daemon(monkeypatch, codex_attached) as (_, server, sent):
        await server.wait_for("hello", timeout=5)
        await server.send(send_frame("claude-send", "s-claude"))
        reply = await reply_to(server, "claude-send", timeout=2)
        assert reply["ok"] is True
        assert sent == ["s-claude"]
        assert not codex_attached.is_set()


async def test_a_codex_send_past_the_wait_is_refused_and_never_carried_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_module, "AGENT_START_WAIT", 0.3)
    codex_attached = asyncio.Event()
    async with starting_daemon(monkeypatch, codex_attached) as (daemon, server, sent):
        await server.wait_for("hello", timeout=5)
        await server.send(send_frame("codex-send", "s-codex"))
        reply = await reply_to(server, "codex-send", timeout=5)
        assert (reply["ok"], reply["error"]["code"]) == (False, "agent_unavailable")

        codex_attached.set()

        async def codex_started() -> None:
            while not daemon._startup.started("codex"):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(codex_started(), timeout=5)
        await asyncio.sleep(0.2)
        # The app gave up on it; nothing of it happens once Codex is there.
        assert sent == []
        await server.send(send_frame("codex-again", "s-codex"))
        assert (await reply_to(server, "codex-again", timeout=5))["ok"] is True
        assert sent == ["s-codex"]


async def test_a_read_never_waits_for_an_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    codex_attached = asyncio.Event()
    async with starting_daemon(monkeypatch, codex_attached) as (_, server, _):
        await server.wait_for("hello", timeout=5)
        await server.send({"type": "session.history", "id": "read", "session_id": "s-codex"})
        assert (await reply_to(server, "read", timeout=2))["ok"] is True
        assert not codex_attached.is_set()


async def test_an_agent_whose_service_fails_is_still_asked_what_it_offers() -> None:
    answers: dict[str, AgentInfo | None] = {}

    async def fail() -> object:
        raise OSError("no socket")

    async def detect(agent: str) -> AgentInfo | None:
        return THIS_RUN.get(agent)

    async def changed() -> None:
        return None

    startup = AgentStartup(
        detect=detect,
        found=answers.__setitem__,
        changed=changed,
        before={"codex": fail},
        after={},
    )
    assert await startup.wait("codex", 0) is True  # nothing is starting it yet
    startup.start()
    assert await startup.wait("codex", 5) is True
    await startup.wait_all(5)
    assert startup.done
    assert answers == {
        "claude": THIS_RUN["claude"],
        "codex": THIS_RUN["codex"],
        "grok": None,
        "pi": None,
    }
    await startup.stop()


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
