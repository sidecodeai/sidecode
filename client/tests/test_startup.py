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
import itertools
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
from rc_client.errors import RcError
from rc_client.gateway import SEND_QUEUE_ITEMS, GatewayLink
from rc_client.models import AgentAccount, AgentInfo, Choice, Session
from rc_client.proxy import DIRECT
from rc_client.registry import Registry
from rc_client.sessions import resume as resume_module
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_codex_daemon_sessions import Harness, RecordingRepair, harness
from tests.test_codex_daemon_sessions import agents as codex_agents
from tests.test_codex_exec_runs import published, write_rollout
from tests.test_first_hello import device_config
from tests.test_gateway_link import FakeGateway, hello_payload

__all__ = ["harness"]

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
    monkeypatch: pytest.MonkeyPatch,
    codex_attached: asyncio.Event,
    *,
    saved: list[dict[str, Any]] | None = LAST_RUN,
    asked: dict[str, asyncio.Event] | None = None,
    resume: str | None = None,
) -> AsyncIterator[tuple[Daemon, FakeGateway, list[str]]]:
    """A restarted daemon whose shared Codex daemon and Grok leader answer only once
    `codex_attached` is set, and the sessions `session.send` was carried out for.

    `saved` is what the last run detected, `asked` holds back the detection of an agent until
    its event is set, and `resume` is a session with a resume already due (A35).
    """
    registry = Registry(config_module.database_path())
    if saved is not None:
        announced.save(registry, saved)
    for session_id, agent in (("s-claude", "claude"), ("s-codex", "codex")):
        registry.upsert_session(
            Session(session_id=session_id, device_id="dev-1", agent=agent, cwd="/repo")
        )
    if resume is not None:
        registry.set_kv(resume_module.PREFERENCE_KEY, "1")
        registry.save_resume(resume, 1, False, 0, 300, "remote")
    registry.close()
    sent: list[str] = []

    async def send(
        self: SessionHub, frame: dict[str, Any], source: str = "remote"
    ) -> dict[str, Any]:
        sent.append(str(frame["session_id"]) + ("" if source == "remote" else f" ({source})"))
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
        gate = (asked or {}).get(agent)
        if gate is not None:
            await gate.wait()
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


async def test_a_claude_request_acts_on_the_last_run_s_answer_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude is a CLI: what it offered when the daemon stopped stands in until it is asked."""
    codex_attached, claude_asked = asyncio.Event(), asyncio.Event()
    async with starting_daemon(monkeypatch, codex_attached, asked={"claude": claude_asked}) as (
        _,
        server,
        sent,
    ):
        await server.wait_for("hello", timeout=5)
        await server.send(send_frame("claude-send", "s-claude"))
        assert (await reply_to(server, "claude-send", timeout=2))["ok"] is True
        assert sent == ["s-claude"]
        assert not claude_asked.is_set()


async def test_a_first_run_hello_waits_for_the_agents_a_while(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing kept from an earlier run, a hello that did not wait would list no agents."""
    monkeypatch.setattr(daemon_module, "FIRST_SCAN_WAIT", 0.1)
    monkeypatch.setattr(daemon_module, "FIRST_AGENTS_WAIT", 3.0)
    codex_attached, claude_asked = asyncio.Event(), asyncio.Event()
    asyncio.get_running_loop().call_later(0.5, claude_asked.set)
    async with starting_daemon(
        monkeypatch, codex_attached, saved=None, asked={"claude": claude_asked}
    ) as (_, server, _):
        hello = await server.wait_for("hello", timeout=5)
        assert [item["agent"] for item in hello["agents"]] == ["claude"]


async def test_a_message_waiting_for_its_agent_still_cancels_a_due_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person got there first (A35), though their message waits for Codex to start."""
    codex_attached = asyncio.Event()
    async with starting_daemon(monkeypatch, codex_attached, resume="s-codex") as (
        daemon,
        server,
        sent,
    ):
        await server.wait_for("hello", timeout=5)
        # Due, but not sent while its agent is still starting.
        await daemon.hub.resumes.tick()
        assert sent == []
        assert daemon.hub.entry("s-codex").session.resume is not None
        await server.send(send_frame("codex-send", "s-codex"))

        async def cancelled() -> None:
            while daemon.hub.entry("s-codex").session.resume is not None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(cancelled(), timeout=2)
        assert not codex_attached.is_set()
        codex_attached.set()
        assert (await reply_to(server, "codex-send", timeout=5))["ok"] is True
        await daemon.hub.resumes.tick()
        assert sent == ["s-codex"]


async def test_a_request_that_comes_while_the_device_stops_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    async def send(self: SessionHub, frame: dict[str, Any]) -> dict[str, Any]:
        sent.append(str(frame["session_id"]))
        return {"accepted": "sent"}

    monkeypatch.setattr(SessionHub, "send", send)
    registry = Registry(config_module.database_path())
    registry.upsert_session(
        Session(session_id="s-claude", device_id="dev-1", agent="claude", cwd="/r")
    )
    registry.close()
    daemon = Daemon(device_config())
    try:
        daemon.hub.load()
        daemon.hub.begin_close()
        with pytest.raises(RcError) as refused:
            await daemon._handlers()["session.send"](send_frame("late", "s-claude"))
        assert refused.value.code == "agent_unavailable"
        assert sent == []
    finally:
        await daemon.hub.close()
        daemon.registry.close()


def test_the_last_run_s_agents_read_back_as_they_were_said() -> None:
    claude = AgentInfo(
        agent="claude",
        available=True,
        version="2.1.325",
        path="/usr/local/bin/claude",
        models=[Choice("opus", "Opus"), Choice("sonnet", "Sonnet")],
        default_model="opus",
        permission_modes=[Choice("default", "Ask")],
        default_permission_mode="default",
        efforts=[Choice("high", "High")],
        default_effort="high",
        speeds=[Choice("fast", "Fast")],
        capabilities=["interrupt", "takeover", "close_terminal"],
        attach="channel",
        attach_ready=True,
        shared_interrupt=True,
        shared_settings=True,
        shared_settings_keys=["model", "effort"],
        shared_attachments=True,
        shared_steer=True,
        accounts=[AgentAccount(provider="anthropic", method="account", plan="max", email="a@b.c")],
    )
    decoded = announced.decode(claude.to_dict())
    assert decoded is not None
    assert decoded.to_dict() == claude.to_dict()
    assert announced.decode({"available": True}) is None


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
    marks: list[int] = []
    read = codex_rollouts.owned_at

    def owned_at(paths: dict[str, list[str]]) -> dict[str, bool]:
        # The batch before has been written: nothing of it waits in the registry's queue.
        assert registry._queued == []
        batches.append(len(paths))
        marks.append(turns)
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
        # The loop went on with other work between one batch and the next.
        assert all(earlier < later for earlier, later in itertools.pairwise(marks))
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


async def test_more_removals_than_the_link_queue_holds_all_reach_the_gateway(
    tmp_path: Path,
) -> None:
    """The link drops what does not fit its queue, so repeats go a queue's worth at a time."""
    server = FakeGateway()
    await server.start()
    link = GatewayLink(server.url, "tok-1", hello=hello_payload, handlers={}, proxy=DIRECT)

    async def publish(frame: dict[str, Any]) -> None:
        await link.send(frame)

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    hub.link_up = lambda: link.connected
    hub.link_room = link.room
    gone = [f"t-{index:05d}" for index in range(SEND_QUEUE_ITEMS + 904)]
    hub._removed = [(0, session_id) for session_id in gone]
    link.start()
    try:
        await asyncio.wait_for(server.ready.wait(), timeout=5)
        hub.snapshot()

        def told() -> list[str]:
            return [frame["session_id"] for frame in server.frames("session.removed")]

        async def all_told() -> None:
            while len(told()) < len(gone):
                await hub.sweep_ghosts()
                await asyncio.sleep(0.05)

        await asyncio.wait_for(all_told(), timeout=20)
        assert sorted(told()) == gone
        assert hub._removed == []
    finally:
        await link.stop()
        await server.stop()
        await hub.close()
        registry.close()


async def test_the_withdrawal_leaves_the_sessions_the_person_took_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exec run the person opened and wrote to from an app is theirs now (A58, A59)."""
    root = tmp_path / "codex-sessions"
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", root)

    async def publish(frame: dict[str, Any]) -> None:
        return None

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    for thread_id in ("t-held", "t-asked", "t-plain"):
        write_rollout(root, thread_id, "codex_exec", "exec")
        published(hub, thread_id)
    hub.entry("t-held").session.held = True
    hub.note_app_request("t-asked")
    try:
        await CodexDaemonService(hub, "0.1.0").prune_foreign()
        assert sorted(hub.entries) == ["t-asked", "t-held"]
    finally:
        await hub.close()
        registry.close()


async def test_the_mirror_reads_codex_rollouts_only_once_the_daemon_s_index_was_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until then a thread the daemon serves reads as a terminal's, held by its own process."""
    asked: list[str] = []

    def discover(*args: Any) -> list[Any]:
        asked.append("codex")
        return []

    monkeypatch.setattr(codex_rollouts, "discover", discover)
    harness.daemon.replies["thread/list"] = {"data": []}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    mirror = MirrorService(harness.hub, codex_daemon=harness.service)
    assert await harness.service.start("/bin/codex") is True
    await mirror.scan_once(settings=False)
    assert asked == []
    await harness.service.refresh()
    await mirror.scan_once(settings=False)
    assert asked == ["codex"]


async def test_a_tick_never_opens_a_second_connection_to_the_daemon(harness: Harness) -> None:
    """The device's own attempt connects while the tick is starting the daemon."""
    switches: list[bool] = []

    async def mode_changed() -> None:
        switches.append(True)

    class SlowRepair(RecordingRepair):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def ensure_running(self) -> bool:
            self.ensured += 1
            await self.release.wait()
            return False

    repair = SlowRepair()
    harness.daemon.replies["thread/list"] = {"data": []}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    service = CodexDaemonService(
        harness.hub, "0.1.0", mode_changed, scan_terminals=harness.terminals, repair=repair
    )
    try:
        tick = asyncio.create_task(service.tick("/bin/codex"))
        await asyncio.sleep(0.05)
        assert repair.ensured == 1
        assert await service.start("/bin/codex") is True
        repair.release.set()
        await tick
        assert [method for method, _ in harness.daemon.calls].count("initialize") == 1
        assert switches == []
    finally:
        await service.stop()


async def test_no_codex_runner_is_built_while_the_daemon_is_being_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon may be serving the thread: a private app-server would write it as well."""

    async def publish(frame: dict[str, Any]) -> None:
        return None

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", codex_agents)
    service = CodexDaemonService(hub, "0.1.0")
    hub.codex_daemon = service
    answer = asyncio.Event()

    async def connect(binary: str | None) -> bool:
        await answer.wait()
        return False

    monkeypatch.setattr(service, "_connect", connect)
    published(hub, "t-1")
    attempt = asyncio.create_task(service.start(None))
    try:
        await asyncio.sleep(0)
        assert service.attempting
        with pytest.raises(RcError) as refused:
            await hub.send({"session_id": "t-1", "text": "go on"})
        assert refused.value.code == "agent_unavailable"
        assert hub.entry("t-1").runner is None
    finally:
        answer.set()
        await attempt
        await hub.close()
        registry.close()
