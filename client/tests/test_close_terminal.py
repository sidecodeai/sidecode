"""Amendment A61: Close for a session a terminal holds.

The device ends the agent's CLI in the terminal, waits at most ten seconds for it
to be gone, and only then files the session, in one publish. A CLI it cannot name
precisely, or one an IDE runs, is `unsupported`; one that does not quit in time is
`conflict`; in both cases nothing changes and nothing is published.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from rc_client import procscan
from rc_client.agents.claude import live
from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.codex.daemon.terminals import TerminalScan
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Choice
from rc_client.procscan import Proc
from rc_client.registry import Registry
from rc_client.sessions import ending
from rc_client.sessions.attach import Attachment
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.ptys import PtyLink, TerminalState
from tests.test_codex_daemon_sessions import THREAD, Harness, harness, started
from tests.test_grok_service import SESSION as GROK_SESSION
from tests.test_grok_service import build, home, leader_peer, register
from tests.test_pi_extension import Bench, bench, connect, settle

__all__ = ["bench", "harness", "home"]


async def cli() -> asyncio.subprocess.Process:
    """A process standing in for the terminal's CLI: it runs until it is ended."""
    return await asyncio.create_subprocess_exec("sleep", "30")


async def stop(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    await process.wait()


def standing_in(process: asyncio.subprocess.Process, command: str) -> Proc:
    return Proc(pid=process.pid, ppid=1, start="s", command=command)


def closes_terminals(hub: SessionHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agents of a test hub, each listing `close_terminal` as a device that has it does."""
    agents = [
        replace(info, capabilities=[*info.capabilities, "close_terminal"]) for info in hub._agents()
    ]
    monkeypatch.setattr(hub, "_agents", lambda: agents)


class Terminal(PtyLink):
    """The pseudo-terminal the shim owns, recording what is typed into it."""

    def __init__(self, pid: int, *, draft: int = 0) -> None:
        super().__init__(pid, cast(asyncio.StreamWriter, _NoSocket()))
        self.typed: list[str] = []
        self.draft = draft
        self.on_exit: asyncio.subprocess.Process | None = None

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        self.typed.append(data)
        if data.startswith("/exit") and self.on_exit is not None:
            self.on_exit.terminate()
        return TerminalState(self.draft, 5.0)

    async def screen(self) -> TerminalState:
        return TerminalState(self.draft, 5.0)

    async def state(self) -> TerminalState:
        return TerminalState(self.draft, 5.0)


class _NoSocket:
    def close(self) -> None:
        return None


class Bridge(Attachment):
    def __init__(self, pid: int) -> None:
        super().__init__(session_id="s-1", cwd="/repo", pid=pid, claude_version="2.1.300")

    async def inject(self, message_id: str, text: str) -> bool:
        return True

    def detach(self) -> None:
        return None


def claude(closes: bool = True) -> list[AgentInfo]:
    capabilities = ["interrupt", "queue", "history"] + (["close_terminal"] if closes else [])
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            capabilities=capabilities,
            attach="channel",
            attach_ready=True,
            shared_interrupt=True,
        )
    ]


class Built:
    def __init__(self, tmp_path: Path, agents: list[AgentInfo]) -> None:
        self.registry = Registry(tmp_path / "state.sqlite3")
        self.frames: list[dict[str, Any]] = []

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", lambda: agents)

    def summaries(self) -> list[dict[str, Any]]:
        return [frame["session"] for frame in self.frames if frame.get("type") == "session.updated"]


@pytest.fixture
async def claude_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Built, asyncio.subprocess.Process]]:
    from rc_client.agents.claude import transcripts

    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(ending, "CLOSE_WAIT", 2.0)
    process = await cli()

    async def found(pid: int | None, expect: Any) -> Proc | None:
        return standing_in(process, "claude") if process.returncode is None else None

    monkeypatch.setattr(ending, "the_process", found)
    built = Built(tmp_path, claude())
    yield built, process
    await stop(process)
    await built.hub.close()
    built.registry.close()


async def attached(built: Built, pid: int, terminal: Terminal | None = None) -> None:
    if terminal is not None:
        built.hub.ptys.add(terminal)
    await built.hub.attach_registered(Bridge(pid))
    entry = built.hub.entry("s-1")
    await entry.channel.emit("user_message", block_id="u1", text="hi", source="terminal")


async def test_an_idle_claude_is_quit_with_exit_and_filed_in_one_publish(
    claude_cli: tuple[Built, asyncio.subprocess.Process],
) -> None:
    built, process = claude_cli
    terminal = Terminal(process.pid)
    terminal.on_exit = process
    await attached(built, process.pid, terminal)
    before = len(built.summaries())

    result = await built.hub.archive({"session_id": "s-1", "archived": True})

    assert terminal.typed == ["/exit\r"]
    session = result["session"]
    assert (session["archived"], session["control"], session["state"]) == (True, "none", "stopped")
    # One summary for the whole close: no step through `none` or `idle` on the way.
    assert [(s["archived"], s["control"], s["state"]) for s in built.summaries()[before:]] == [
        (True, "none", "stopped")
    ]
    assert built.hub.entry("s-1").shared is None


async def test_a_claude_with_words_in_its_prompt_box_is_ended_with_a_signal(
    claude_cli: tuple[Built, asyncio.subprocess.Process], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, process = claude_cli
    terminal = Terminal(process.pid, draft=5)
    await attached(built, process.pid, terminal)
    signalled: list[int] = []
    real = procscan.terminate

    async def terminate(pid: int, identity: tuple[int, str], **kwargs: Any) -> bool:
        signalled.append(pid)
        process.terminate()
        return await real(pid, (pid, "s"), deadline=kwargs["deadline"], expect=None)

    monkeypatch.setattr(procscan, "terminate", terminate)
    result = await built.hub.archive({"session_id": "s-1", "archived": True})

    assert terminal.typed == []
    assert signalled == [process.pid]
    assert result["session"]["archived"] is True


async def test_a_claude_an_ide_runs_is_not_the_device_s_to_close(
    claude_cli: tuple[Built, asyncio.subprocess.Process], tmp_path: Path
) -> None:
    built, process = claude_cli
    await attached(built, process.pid)
    records = claude_runtime.CLAUDE_HOME / "sessions"
    records.mkdir(parents=True)
    (records / f"{process.pid}.json").write_text(
        json.dumps(
            {
                "pid": process.pid,
                "sessionId": "s-1",
                "procStart": "s",
                "entrypoint": "claude-vscode",
            }
        ),
        encoding="utf-8",
    )
    assert live.read_records()[process.pid].entrypoint == "claude-vscode"
    before = len(built.frames)

    with pytest.raises(RcError) as refused:
        await built.hub.archive({"session_id": "s-1", "archived": True})

    assert refused.value.code == "unsupported"
    assert built.frames[before:] == []
    assert built.hub.entry("s-1").session.control == "shared"
    assert process.returncode is None


async def test_an_agent_that_cannot_close_terminals_refuses_and_changes_nothing(
    tmp_path: Path,
) -> None:
    built = Built(tmp_path, claude(closes=False))
    await built.hub.attach_registered(Bridge(4242))
    before = len(built.frames)
    with pytest.raises(RcError) as refused:
        await built.hub.archive({"session_id": "s-1", "archived": True})
    assert refused.value.code == "unsupported"
    assert built.frames[before:] == []
    assert built.hub.entry("s-1").session.archived is False
    await built.hub.close()
    built.registry.close()


async def test_a_cli_that_does_not_quit_in_time_is_a_conflict(
    claude_cli: tuple[Built, asyncio.subprocess.Process], monkeypatch: pytest.MonkeyPatch
) -> None:
    built, process = claude_cli
    monkeypatch.setattr(ending, "CLOSE_WAIT", 0.3)
    terminal = Terminal(process.pid)
    await attached(built, process.pid, terminal)
    before = len(built.frames)

    with pytest.raises(RcError) as refused:
        await built.hub.archive({"session_id": "s-1", "archived": True})

    assert refused.value.code == "conflict"
    assert terminal.typed == ["/exit\r"]
    assert [f for f in built.frames[before:] if f.get("type") == "session.updated"] == []
    assert built.hub.entry("s-1").session.archived is False


# --------------------------------------------------------------------- codex


async def test_a_shared_codex_thread_closes_only_with_one_tui_in_its_directory(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    closes_terminals(harness.hub, monkeypatch)
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    assert entry.session.control == "shared"
    process = await cli()
    tui = Proc(pid=process.pid, ppid=1, start="s", command="codex")
    scans = [TerminalScan(cwds={"/repo": 2}, complete=True, tuis={"/repo": (tui, tui)})]

    async def scan() -> TerminalScan:
        return scans[-1]

    async def found(pid: int | None, expect: Any) -> Proc | None:
        return tui if process.returncode is None else None

    async def terminate(pid: int, identity: tuple[int, str], **kwargs: Any) -> bool:
        process.terminate()
        await process.wait()
        return True

    from rc_client.agents.codex.daemon import terminals

    monkeypatch.setattr(terminals, "scan_terminals", scan)
    monkeypatch.setattr(ending, "the_process", found)
    monkeypatch.setattr(procscan, "terminate", terminate)
    try:
        with pytest.raises(RcError) as refused:
            await harness.hub.archive({"session_id": THREAD, "archived": True})
        assert refused.value.code == "unsupported"
        assert entry.session.control == "shared"

        scans.append(TerminalScan(cwds={"/repo": 1}, complete=True, tuis={"/repo": (tui,)}))
        result = await harness.hub.archive({"session_id": THREAD, "archived": True})
        session = result["session"]
        assert (session["archived"], session["control"]) == (True, "none")
        # A thread a terminal started stays in `codex resume`.
        asked = [method for method, _ in harness.daemon.calls]
        assert "thread/archive" not in asked
        assert "thread/unsubscribe" in asked
    finally:
        await stop(process)


# ------------------------------------------------------------------------ pi


async def test_a_terminal_pi_is_asked_to_shut_down(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = await cli()

    async def found(pid: int | None, expect: Any) -> Proc | None:
        return standing_in(process, "pi") if process.returncode is None else None

    monkeypatch.setattr(ending, "the_process", found)
    closes_terminals(bench.hub, monkeypatch)
    extension = await connect(bench)
    await extension.hello(pid=process.pid)
    await settle()

    closing = asyncio.create_task(
        bench.hub.archive({"session_id": bench.entry().session.session_id, "archived": True})
    )
    command = await extension.receive()
    assert (command["type"], command["command"]) == ("command", "shutdown")
    await extension.send(
        {"type": "reply", "id": command["id"], "ok": True, "data": None, "error": None}
    )
    await extension.send({"type": "bye", "reason": "quit"})
    process.terminate()
    result = await asyncio.wait_for(closing, timeout=5)
    assert (result["session"]["archived"], result["session"]["control"]) == (True, "none")
    await stop(process)


async def test_an_older_pi_extension_is_ended_with_a_signal(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = await cli()

    async def found(pid: int | None, expect: Any) -> Proc | None:
        return standing_in(process, "pi") if process.returncode is None else None

    async def terminate(pid: int, identity: tuple[int, str], **kwargs: Any) -> bool:
        process.terminate()
        await process.wait()
        return True

    monkeypatch.setattr(ending, "the_process", found)
    monkeypatch.setattr(procscan, "terminate", terminate)
    closes_terminals(bench.hub, monkeypatch)
    extension = await connect(bench)
    await extension.hello(pid=process.pid)
    await settle()

    session_id = bench.entry().session.session_id
    closing = asyncio.create_task(bench.hub.archive({"session_id": session_id, "archived": True}))
    command = await extension.receive()
    await extension.send(
        {
            "type": "reply",
            "id": command["id"],
            "ok": False,
            "data": None,
            "error": "unknown command shutdown",
        }
    )
    result = await asyncio.wait_for(closing, timeout=5)
    assert result["session"]["archived"] is True
    assert process.returncode is not None


# ------------------------------------------------------------ what may be ended


async def test_only_this_user_s_cli_on_a_terminal_is_ever_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procs = [
        Proc(pid=10, ppid=1, start="a", command="/usr/local/bin/claude"),
        Proc(pid=11, ppid=1, start="b", command="/usr/local/bin/claude", has_tty=False),
        Proc(pid=12, ppid=1, start="c", command="/usr/bin/vim"),
        Proc(pid=13, ppid=1, start="d", command="/usr/local/bin/claude"),
    ]

    async def scan() -> procscan.Scan:
        return procscan.Scan(procs=procs, complete=True)

    monkeypatch.setattr(procscan, "scan_processes", scan)
    monkeypatch.setattr(
        procscan, "process_uid", lambda pid: os.getuid() if pid != 13 else os.getuid() + 1
    )
    from rc_client.agents.claude.holders import looks_like_claude

    found = await ending.the_process(10, looks_like_claude)
    assert found is not None and found.pid == 10
    assert await ending.the_process(99, looks_like_claude) is None
    for pid in (11, 12, 13, None):
        with pytest.raises(RcError) as refused:
            await ending.the_process(pid, looks_like_claude)
        assert refused.value.code == "unsupported"


# ---------------------------------------------------------------------- grok


async def test_a_grok_tui_is_ended_and_its_session_unloaded_once_it_is_gone(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = await cli()
    register(home, pid=process.pid)
    binary, peer = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    closes_terminals(hub, monkeypatch)
    signalled: list[int] = []

    async def found(pid: int | None, expect: Any) -> Proc | None:
        if pid != process.pid or process.returncode is not None:
            return None
        return standing_in(process, "grok")

    async def terminate(pid: int, identity: tuple[int, str], **kwargs: Any) -> bool:
        signalled.append(pid)
        process.terminate()
        await process.wait()
        return True

    monkeypatch.setattr(ending, "the_process", found)
    monkeypatch.setattr(procscan, "terminate", terminate)
    try:
        await service.tick(binary)
        assert str(hub.entries[GROK_SESSION].session.control) == "shared"
        assert "session/close" not in peer.methods()

        result = await hub.archive({"session_id": GROK_SESSION, "archived": True})

        assert signalled == [process.pid]
        # Only once the TUI is gone is the session unloaded for every client of the leader.
        assert "session/close" in peer.methods()
        session = result["session"]
        assert (session["archived"], session["control"], session["state"]) == (
            True,
            "none",
            "stopped",
        )
    finally:
        await service.stop()
        await stop(process)


async def test_a_tui_that_names_its_thread_is_the_one_ended_wherever_it_runs(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    closes_terminals(harness.hub, monkeypatch)
    await started(harness, loaded=[THREAD])
    process = await cli()
    other = Proc(pid=process.pid + 100000, ppid=1, start="o", command="codex")
    resumed = Proc(pid=process.pid, ppid=1, start="s", command=f"codex resume {THREAD}")
    # Another thread's TUI sits in the thread's directory; the one resuming it runs elsewhere.
    scan = TerminalScan(
        cwds={"/repo": 1, "/elsewhere": 1},
        complete=True,
        tuis={"/repo": (other,), "/elsewhere": (resumed,)},
    )
    ended: list[int] = []

    async def terminals_now() -> TerminalScan:
        return scan

    async def found(pid: int | None, expect: Any) -> Proc | None:
        return resumed if pid == process.pid and process.returncode is None else None

    async def terminate(pid: int, identity: tuple[int, str], **kwargs: Any) -> bool:
        ended.append(pid)
        process.terminate()
        await process.wait()
        return True

    from rc_client.agents.codex.daemon import terminals

    monkeypatch.setattr(terminals, "scan_terminals", terminals_now)
    monkeypatch.setattr(ending, "the_process", found)
    monkeypatch.setattr(procscan, "terminate", terminate)
    try:
        result = await harness.hub.archive({"session_id": THREAD, "archived": True})
        assert ended == [process.pid]
        assert result["session"]["archived"] is True
    finally:
        await stop(process)


async def test_the_one_tui_in_the_directory_is_not_taken_when_it_resumes_another_thread(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    closes_terminals(harness.hub, monkeypatch)
    await started(harness, loaded=[THREAD])
    elsewhere = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
    tui = Proc(pid=4321, ppid=1, start="s", command=f"codex resume {elsewhere}")

    async def terminals_now() -> TerminalScan:
        return TerminalScan(cwds={"/repo": 1}, complete=True, tuis={"/repo": (tui,)})

    from rc_client.agents.codex.daemon import terminals

    monkeypatch.setattr(terminals, "scan_terminals", terminals_now)
    with pytest.raises(RcError) as refused:
        await harness.hub.archive({"session_id": THREAD, "archived": True})
    assert refused.value.code == "unsupported"
    assert harness.hub.entry(THREAD).session.archived is False
