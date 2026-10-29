"""The whole daemon, wired to a fake gateway over a real WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from rc_client import attachments
from rc_client import daemon as daemon_module
from rc_client.agents.base import SessionRunner
from rc_client.agents.claude import transcripts
from rc_client.agents.codex import rollouts
from rc_client.agents.registry import AGENT_IDS
from rc_client.build import write_build
from rc_client.channel import paths
from rc_client.config import Config, ensure_dirs
from rc_client.daemon import Daemon
from rc_client.models import Session
from rc_client.sessions.attach import Attachment, HookQuestion
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.shared import SharedApproval, SharedQuestion, SharedState
from rc_client.update import update_log_path
from tests.test_gateway_link import FakeGateway


@pytest.fixture
async def running_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Daemon, FakeGateway]]:
    # Point the mirroring roots at empty directories: these tests exercise the
    # daemon wiring, not the transcripts that happen to exist on this machine.
    empty = tmp_path / "agent-state"
    empty.mkdir()
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", empty / "claude")
    monkeypatch.setattr(rollouts, "SESSIONS_DIR", empty / "codex")
    server = FakeGateway()
    await server.start()
    config = Config(
        gateway_origin="http://127.0.0.1:8787",
        device_id="dev-1",
        device_token="tok-1",
        name="test-device",
    )
    daemon = Daemon(config)
    daemon.link.url = server.url
    task = asyncio.create_task(daemon.run())
    try:
        await asyncio.wait_for(server.ready.wait(), timeout=10)

        async def settle() -> None:
            while not daemon.link.connected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(settle(), timeout=10)
        yield daemon, server
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await server.stop()


async def test_the_attachment_sockets_open_before_the_agents_are_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLIs that outlived the last daemon dial back from the moment it went, and adopting
    Codex's threads and joining Grok's leader takes seconds they need not wait out."""
    empty = tmp_path / "agent-state"
    empty.mkdir()
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", empty / "claude")
    monkeypatch.setattr(rollouts, "SESSIONS_DIR", empty / "codex")
    server = FakeGateway()
    await server.start()
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
        )
    )
    daemon.link.url = server.url
    order: list[str] = []
    prepare = daemon._prepare_attachment

    async def sockets() -> None:
        order.append("sockets")
        await prepare()

    async def adopt(binary: str | None) -> bool:
        order.append("codex")
        return False

    async def join(binary: str | None) -> None:
        order.append("grok")

    monkeypatch.setattr(daemon, "_prepare_attachment", sockets)
    monkeypatch.setattr(daemon.codex, "start", adopt)
    monkeypatch.setattr(daemon.grok, "ensure", join)
    task = asyncio.create_task(daemon.run())
    try:

        async def started() -> None:
            while daemon._refresh_task is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(started(), timeout=10)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await server.stop()

    # The scan tries Codex again every round, so only how it began says what came first.
    assert order[:3] == ["sockets", "codex", "grok"]


async def request(server: FakeGateway, frame: dict[str, Any]) -> dict[str, Any]:
    await server.send(frame)

    async def poll() -> dict[str, Any]:
        while True:
            for reply in server.frames("reply"):
                if reply.get("id") == frame["id"]:
                    return reply
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=15)


async def test_hello_describes_this_device_and_its_agents(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    hello = server.frames("hello")[0]
    assert hello["protocol"] == 1
    assert hello["name"] == "test-device"
    assert hello["platform"] in {"macos", "linux"}
    assert {info["agent"] for info in hello["agents"]} == set(AGENT_IDS)
    assert isinstance(hello["sessions"], list)
    assert "device_token" not in hello


@pytest.fixture
def stale_hook_files() -> None:
    """The settings files an earlier installation left behind: a `Stop` hook and nothing else."""
    ensure_dirs()
    stale = json.dumps({"hooks": {"Stop": []}})
    for path in (paths.settings_path(), paths.no_batch_settings_path()):
        path.write_text(stale, encoding="utf-8")


async def test_startup_refreshes_the_hook_files_the_shim_points_at(
    stale_hook_files: None, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    """A newly added hook reaches every CLI started from now on, with no reinstall."""
    full = json.loads(paths.settings_path().read_text())["hooks"]
    lean = json.loads(paths.no_batch_settings_path().read_text())["hooks"]
    newer = {"PostToolBatch", "SubagentStart", "SubagentStop", "StopFailure"}
    assert newer <= set(full)
    assert not newer & set(lean)
    assert {"UserPromptSubmit", "Stop"} <= set(lean)


async def test_device_dirs_is_answered_over_the_socket(
    running_daemon: tuple[Daemon, FakeGateway], tmp_path: Path
) -> None:
    _, server = running_daemon
    root = tmp_path / "workspace"
    (root / "project").mkdir(parents=True)
    (root / ".hidden").mkdir()
    reply = await request(
        server,
        {
            "type": "device.dirs",
            "id": "r1",
            "from": "app-1",
            "device_id": "dev-1",
            "path": str(root),
        },
    )
    assert reply["ok"] is True
    assert reply["from"] == "app-1"
    assert [entry["name"] for entry in reply["result"]["entries"]] == ["project"]
    assert reply["result"]["path"] == str(root)


async def test_device_mkdir_makes_the_folder_and_replies_with_its_listing(
    running_daemon: tuple[Daemon, FakeGateway], tmp_path: Path
) -> None:
    _, server = running_daemon
    root = tmp_path / "workspace"
    root.mkdir()
    reply = await request(
        server,
        {
            "type": "device.mkdir",
            "id": "m1",
            "from": "app-1",
            "device_id": "dev-1",
            "path": str(root),
            "name": "new-project",
        },
    )
    assert reply["ok"] is True
    assert (root / "new-project").is_dir()
    assert reply["result"]["path"] == str(root / "new-project")
    assert reply["result"]["parent"] == str(root)
    assert reply["result"]["entries"] == []


async def test_device_mkdir_reports_an_existing_folder_as_a_conflict(
    running_daemon: tuple[Daemon, FakeGateway], tmp_path: Path
) -> None:
    _, server = running_daemon
    (tmp_path / "taken").mkdir()
    reply = await request(
        server,
        {
            "type": "device.mkdir",
            "id": "m2",
            "from": "app-1",
            "device_id": "dev-1",
            "path": str(tmp_path),
            "name": "taken",
        },
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "conflict"


async def test_device_mkdir_requires_a_path_and_a_name(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await request(
        server, {"type": "device.mkdir", "id": "m3", "from": "app-1", "device_id": "dev-1"}
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"


async def test_device_git_reports_a_non_repository(
    running_daemon: tuple[Daemon, FakeGateway], tmp_path: Path
) -> None:
    _, server = running_daemon
    reply = await request(
        server,
        {
            "type": "device.git",
            "id": "r2",
            "from": "app-1",
            "device_id": "dev-1",
            "path": str(tmp_path),
        },
    )
    assert reply["result"] == {"is_repo": False}


async def test_device_git_requires_a_path(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await request(
        server, {"type": "device.git", "id": "r3", "from": "app-1", "device_id": "dev-1"}
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"


async def test_device_agents_re_detects_and_returns_the_agent_list(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await request(
        server, {"type": "device.agents", "id": "r4", "from": "app-1", "device_id": "dev-1"}
    )
    agents = reply["result"]["agents"]
    assert {info["agent"] for info in agents} == set(AGENT_IDS)
    for info in agents:
        assert isinstance(info["capabilities"], list)


async def test_requests_for_unknown_sessions_are_not_found(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await request(
        server,
        {"type": "session.history", "id": "r5", "from": "app-1", "session_id": "nope"},
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "not_found"


async def test_session_events_reach_the_gateway_unsolicited(
    running_daemon: tuple[Daemon, FakeGateway], tmp_path: Path
) -> None:
    daemon, server = running_daemon
    session = Session(
        session_id="1f0a1c1e-2b3d-4c5f-8a9b-0c1d2e3f4a5b",
        device_id="dev-1",
        agent="claude",
        cwd=str(tmp_path),
    )
    entry = daemon.hub.register_mirrored(session)
    await entry.channel.emit("notice", level="info", text="mirrored a terminal session")
    await entry.channel.publish_summary()

    async def poll() -> dict[str, Any]:
        while True:
            for frame in server.frames("session.event"):
                if frame["session_id"] == session.session_id:
                    return frame
            await asyncio.sleep(0.01)

    event = await asyncio.wait_for(poll(), timeout=10)
    assert event["session_id"] == session.session_id
    assert event["event"]["kind"] == "notice"
    assert event["event"]["seq"] == 1

    summary = next(
        frame
        for frame in reversed(server.frames("session.updated"))
        if frame["session"]["session_id"] == session.session_id
    )
    assert summary["session"]["device_id"] == "dev-1"
    assert summary["session"]["last_seq"] == 1


async def test_history_and_send_idempotency_survive_over_the_socket(
    running_daemon: tuple[Daemon, FakeGateway], tmp_path: Path
) -> None:
    daemon, server = running_daemon
    session = Session(
        session_id="2f0a1c1e-2b3d-4c5f-8a9b-0c1d2e3f4a5c",
        device_id="dev-1",
        agent="claude",
        cwd=str(tmp_path),
        control="terminal",
    )
    daemon.hub.register_mirrored(session)

    reply = await request(
        server,
        {
            "type": "session.send",
            "id": "r6",
            "from": "app-1",
            "session_id": session.session_id,
            "text": "go",
            "mode": "auto",
        },
    )
    assert reply["error"]["code"] == "conflict"

    history = await request(
        server,
        {
            "type": "session.history",
            "id": "r7",
            "from": "app-1",
            "session_id": session.session_id,
            "limit": 10,
        },
    )
    assert history["result"] == {"events": [], "has_more": False}


# ------------------------------------------------------- amendment A22: update

RUNNING_BUILD = "a" * 64
SERVED_BUILD = "b" * 64


class FakeUpdater:
    """Stands in for the detached `rc-client self-update` process."""

    def __init__(self, code: int) -> None:
        self.code = code

    async def wait(self) -> int:
        return self.code


@pytest.fixture
def recorded_build() -> str:
    """The build file the installer writes, in place before the daemon starts."""
    write_build(RUNNING_BUILD)
    return RUNNING_BUILD


async def update_request(server: FakeGateway, build: str, request_id: str) -> dict[str, Any]:
    return await request(
        server,
        {
            "type": "device.update",
            "id": request_id,
            "from": "app-1",
            "device_id": "dev-1",
            "build": build,
        },
    )


async def test_hello_reports_no_build_when_the_client_came_from_source(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    assert server.frames("hello")[0]["client_build"] is None


async def test_hello_reports_the_build_the_installer_recorded(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    _, server = running_daemon
    assert server.frames("hello")[0]["client_build"] == recorded_build


async def test_an_update_is_unsupported_without_a_recorded_build(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await update_request(server, SERVED_BUILD, "u1")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "unsupported"


async def test_an_update_to_the_build_already_running_is_a_conflict(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    _, server = running_daemon
    reply = await update_request(server, recorded_build, "u2")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "conflict"
    assert reply["error"]["message"] == "already on this build"


async def test_a_malformed_build_is_a_bad_request(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    _, server = running_daemon
    reply = await update_request(server, "not-a-digest", "u3")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"


async def test_an_update_is_refused_while_a_session_is_working(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    daemon, server = running_daemon
    session = Session(
        session_id="sess-busy",
        device_id="dev-1",
        agent="claude",
        cwd="/repo",
        state="running",
    )
    daemon.registry.upsert_session(session)
    entry = SessionEntry(
        session=session,
        channel=SessionChannel(daemon.registry, session, daemon.hub.publish),
    )
    # Only its presence matters here: the refusal counts entries that have one.
    entry.runner = cast(SessionRunner, object())
    daemon.hub.entries[session.session_id] = entry

    reply = await update_request(server, SERVED_BUILD, "u4")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "conflict"
    assert reply["error"]["message"] == "1 session is running"


def attached_session(daemon: Daemon, session_id: str = "sess-attached") -> SessionEntry:
    """A session a terminal CLI shares with the device, at rest: nothing running, nothing held."""
    session = Session(
        session_id=session_id,
        device_id="dev-1",
        agent="claude",
        cwd="/repo",
        state="idle",
        origin="terminal",
        control="shared",
    )
    daemon.registry.upsert_session(session)
    entry = SessionEntry(
        session=session,
        channel=SessionChannel(daemon.registry, session, daemon.hub.publish),
    )
    entry.shared = SharedState(
        attachment=Attachment(
            session_id=session_id, cwd="/repo", pid=4242, claude_version="2.1.311"
        )
    )
    daemon.hub.entries[session_id] = entry
    return entry


class FakeTerminal:
    """Stands in for one of the shells the device runs; only its presence counts."""

    async def shutdown(self) -> None:
        return None


def an_approval() -> SharedApproval:
    return SharedApproval(
        request_id="r-1", channel_id="c-1", block_id="b-1", tool="Bash", title="run", input={}
    )


def a_question(session_id: str) -> SharedQuestion:
    hook = HookQuestion(session_id=session_id, cwd="/repo", tool="AskUserQuestion", input={})
    return SharedQuestion(request_id="q-1", block_id="b-2", questions=[], hook=hook)


def hold_a_message(entry: SessionEntry) -> None:
    entry.queue.append({"id": "m-1", "text": "next", "ts": 1, "attachments": []})


def steer_a_message(entry: SessionEntry) -> None:
    assert entry.shared is not None
    entry.shared.steers.append({"id": "m-2", "text": "now", "ts": 1})


def open_a_turn(entry: SessionEntry) -> None:
    entry.session.turn = {"turn_id": "t-1", "started_at": 1}


def be_running(entry: SessionEntry) -> None:
    entry.session.state = "running"


def be_asked_for_approval(entry: SessionEntry) -> None:
    assert entry.shared is not None
    entry.shared.approvals["r-1"] = an_approval()


def be_asked_a_question(entry: SessionEntry) -> None:
    assert entry.shared is not None
    entry.shared.question = a_question(entry.session.session_id)


@pytest.mark.parametrize(
    "occupy",
    [
        open_a_turn,
        be_running,
        be_asked_for_approval,
        be_asked_a_question,
        hold_a_message,
        steer_a_message,
    ],
)
async def test_an_update_waits_for_an_attached_session_that_is_in_use(
    recorded_build: str,
    running_daemon: tuple[Daemon, FakeGateway],
    occupy: Callable[[SessionEntry], None],
) -> None:
    """A turn open, a prompt on screen or a message waiting: the restart would be felt."""
    daemon, server = running_daemon
    occupy(attached_session(daemon))

    reply = await update_request(server, SERVED_BUILD, "u-busy")

    assert reply["ok"] is False
    assert reply["error"] == {"code": "conflict", "message": "1 session is running"}


async def test_an_update_counts_every_session_in_use(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    daemon, server = running_daemon
    open_a_turn(attached_session(daemon, "sess-a"))
    hold_a_message(attached_session(daemon, "sess-b"))
    attached_session(daemon, "sess-c")

    reply = await update_request(server, SERVED_BUILD, "u-count")

    assert reply["error"]["message"] == "2 sessions are running"


async def test_an_update_waits_for_a_held_message_on_a_session_the_device_drives(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    daemon, server = running_daemon
    entry = attached_session(daemon)
    entry.shared = None
    entry.session.control = "remote"
    entry.runner = cast(SessionRunner, object())
    hold_a_message(entry)

    reply = await update_request(server, SERVED_BUILD, "u-queue")

    assert reply["error"]["message"] == "1 session is running"


@pytest.mark.parametrize(
    ("opened", "said"), [(1, "1 terminal is open"), (2, "2 terminals are open")]
)
async def test_an_update_waits_for_a_device_terminal(
    recorded_build: str, running_daemon: tuple[Daemon, FakeGateway], opened: int, said: str
) -> None:
    """A38: its shell ends with the daemon, and the gateway's index cannot show it."""
    daemon, server = running_daemon
    for number in range(opened):
        daemon.terminals._terminals[f"term-{number}"] = cast(Any, FakeTerminal())

    reply = await update_request(server, SERVED_BUILD, "u-terminal")

    assert reply["error"] == {"code": "conflict", "message": said}


async def test_an_update_is_not_held_by_messages_queued_on_a_session_nothing_holds(
    recorded_build: str,
    running_daemon: tuple[Daemon, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They wait for the next send to resume the session, which an update does not change."""
    daemon, server = running_daemon
    dormant = attached_session(daemon)
    dormant.shared = None
    dormant.session.control = "none"
    hold_a_message(dormant)

    async def spawn(build: str) -> FakeUpdater:
        return FakeUpdater(0)

    monkeypatch.setattr(daemon_module, "spawn_self_update", spawn)
    reply = await update_request(server, SERVED_BUILD, "u-dormant")

    assert reply["result"] == {"accepted": True, "from": recorded_build}


async def test_an_update_is_accepted_while_nothing_is_in_use(
    recorded_build: str,
    running_daemon: tuple[Daemon, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attached session at rest, and a session the device drives that is idle, do not hold it."""
    daemon, server = running_daemon
    attached_session(daemon)
    resting = attached_session(daemon, "sess-driven")
    resting.shared = None
    resting.session.control = "remote"
    resting.runner = cast(SessionRunner, object())

    async def spawn(build: str) -> FakeUpdater:
        return FakeUpdater(0)

    monkeypatch.setattr(daemon_module, "spawn_self_update", spawn)
    reply = await update_request(server, SERVED_BUILD, "u-idle")

    assert reply["result"] == {"accepted": True, "from": recorded_build}


async def test_an_accepted_update_spawns_the_updater(
    recorded_build: str,
    running_daemon: tuple[Daemon, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, server = running_daemon
    spawned: list[str] = []

    async def spawn(build: str) -> FakeUpdater:
        spawned.append(build)
        return FakeUpdater(0)

    monkeypatch.setattr(daemon_module, "spawn_self_update", spawn)
    reply = await update_request(server, SERVED_BUILD, "u5")
    assert reply["result"] == {"accepted": True, "from": recorded_build}

    async def settle() -> None:
        while not spawned:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(settle(), timeout=5)
    assert spawned == [SERVED_BUILD]
    assert server.frames("update.failed") == []


async def test_an_updater_that_exits_non_zero_reports_the_last_log_line(
    recorded_build: str,
    running_daemon: tuple[Daemon, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, server = running_daemon
    ensure_dirs()
    update_log_path().write_text("downloading\nerror: uv is not installed\n", encoding="utf-8")

    async def spawn(build: str) -> FakeUpdater:
        return FakeUpdater(1)

    monkeypatch.setattr(daemon_module, "spawn_self_update", spawn)
    assert (await update_request(server, SERVED_BUILD, "u6"))["ok"] is True
    failure = await server.wait_for("update.failed")
    assert failure["message"] == "error: uv is not installed"


async def test_a_second_update_while_one_runs_is_a_conflict(
    recorded_build: str,
    running_daemon: tuple[Daemon, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, server = running_daemon
    started = asyncio.Event()
    release = asyncio.Event()

    async def spawn(build: str) -> FakeUpdater:
        started.set()
        await release.wait()
        return FakeUpdater(0)

    monkeypatch.setattr(daemon_module, "spawn_self_update", spawn)
    assert (await update_request(server, SERVED_BUILD, "u7"))["ok"] is True
    await asyncio.wait_for(started.wait(), timeout=5)

    reply = await update_request(server, "d" * 64, "u8")
    assert reply["error"]["message"] == "an update is already running"
    release.set()


# ------------------------------------------------- A49: the attachments sweep


@pytest.fixture
def week_old_attachment() -> Path:
    """A file an earlier run left, there before the daemon starts."""
    path = attachments.state_root() / "sess-old" / "aaaaaaaaaaaa" / "shot.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")
    moment = time.time() - 8 * 24 * 3600
    os.utime(path, (moment, moment))
    return path


async def test_the_attachments_are_swept_as_the_daemon_starts(
    week_old_attachment: Path, running_daemon: tuple[Daemon, FakeGateway]
) -> None:
    async def gone() -> None:
        while week_old_attachment.exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(gone(), timeout=5)


async def test_the_sweep_repeats_off_the_event_loop_and_outlives_a_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    threads: list[bool] = []

    def sweep() -> attachments.Swept:
        threads.append(threading.current_thread() is threading.main_thread())
        if len(threads) == 1:
            raise PermissionError("/somewhere/private/holiday.png")
        return attachments.Swept()

    monkeypatch.setattr(attachments, "sweep", sweep)
    monkeypatch.setattr(attachments, "SWEEP_INTERVAL", 0.01)
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
        )
    )
    task = asyncio.create_task(daemon._sweep_loop())

    async def repeated() -> None:
        while len(threads) < 3:
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(repeated(), timeout=5)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        daemon.registry.close()
    assert not any(threads), "a sweep never runs on the event loop's thread"
    [failure] = [
        record for record in caplog.records if record.getMessage() == "attachment sweep failed"
    ]
    assert failure.__dict__["context"] == {"error": "PermissionError"}, "and names no file"
