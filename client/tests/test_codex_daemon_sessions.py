"""Amendment A11: Codex sessions driven through the shared app-server daemon."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex import provenance
from rc_client.agents.codex.daemon import approvals, children, terminals, threads
from rc_client.agents.codex.daemon.repair import DaemonRepair, SafetyCheck
from rc_client.agents.codex.daemon.service import CodexDaemonService, thread_config
from rc_client.agents.codex.daemon.session import CodexDaemonSession
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Choice, Session
from rc_client.procscan import OpenPaths, Proc, Scan
from rc_client.registry import Registry
from rc_client.sessions import titles
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.fake_codex_daemon import FakeDaemon, FakeTerminals
from tests.helpers import drained

THREAD = "01a08bde-23d6-7262-a563-68dcfb2c4b59"


def agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="codex",
            available=True,
            path="/bin/codex",
            models=[Choice("gpt-5.4-codex", "GPT-5.4 Codex")],
            default_model="gpt-5.4-codex",
            permission_modes=[Choice("on-request", "Ask when needed")],
            default_permission_mode="on-request",
            efforts=[Choice("low", "Low"), Choice("medium", "Medium")],
            capabilities=["interrupt", "queue", "steer", "history", "attachments", "effort"],
            attach="daemon",
            attach_ready=True,
            shared_interrupt=True,
            shared_settings=True,
            shared_attachments=True,
        )
    ]


def thread_row(thread_id: str = THREAD, **extra: Any) -> dict[str, Any]:
    row = {
        "id": thread_id,
        "sessionId": thread_id,
        "cwd": "/repo",
        "preview": "Typecheck the web app",
        "ephemeral": False,
        # Codex counts seconds here; the protocol counts milliseconds.
        "createdAt": 1788946200,
        "updatedAt": 1788946241,
        "model": "gpt-5.4-codex",
        "reasoningEffort": "medium",
        "status": {"type": "idle"},
        # A thread this device opened through the shared daemon, which is what
        # the index says about one it is allowed to publish (A18).
        "originator": provenance.DAEMON_CLIENT_NAME,
        "source": "vscode",
    }
    row.update(extra)
    return row


class Harness:
    """A hub with the daemon service wired to an in-process fake daemon."""

    def __init__(self, tmp_path: Path, daemon: FakeDaemon) -> None:
        self.frames: list[dict[str, Any]] = []
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", agents)
        # A terminal is sitting in the threads' directory until a test says otherwise.
        self.terminals = FakeTerminals({"/repo"})
        self.service = CodexDaemonService(self.hub, "0.1.0", scan_terminals=self.terminals)
        self.hub.codex_daemon = self.service
        self.daemon = daemon

    def bubbles(self) -> dict[str, str]:
        """`block_id -> source` for the user messages, one entry per bubble.

        The daemon sends the same item on `item/started` and `item/completed`,
        and both events carry the same block id, so the apps show one bubble.
        """
        return {event["block_id"]: event["source"] for event in self.events("user_message")}

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
async def harness(tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
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


async def started(
    harness: Harness, loaded: list[str] | None = None, *, row: dict[str, Any] | None = None
) -> None:
    row = row if row is not None else thread_row()
    harness.daemon.replies["thread/list"] = {"data": [row]}
    harness.daemon.replies["thread/loaded/list"] = {"data": loaded if loaded is not None else []}
    harness.daemon.replies["thread/resume"] = {
        "thread": row,
        "model": "gpt-5.4-codex",
        "reasoningEffort": "medium",
        "approvalPolicy": "on-request",
    }
    harness.daemon.replies["thread/items/list"] = {"data": []}
    harness.daemon.replies["turn/start"] = {"turn": {"id": "turn-1"}}
    harness.daemon.replies["turn/steer"] = {"turnId": "turn-1"}
    assert await harness.service.start("/bin/codex") is True


async def settle(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# ------------------------------------------------------------ origin/control


def test_the_amendment_table_maps_every_situation() -> None:
    assert threads.resolve(created_here=True, loaded=True, terminal_holds=False) == (
        "remote",
        "remote",
    )
    assert threads.resolve(created_here=True, loaded=True, terminal_holds=True) == (
        "remote",
        "shared",
    )
    assert threads.resolve(created_here=False, loaded=True, terminal_holds=True) == (
        "terminal",
        "shared",
    )
    assert threads.resolve(created_here=False, loaded=False, terminal_holds=True) == (
        "terminal",
        "none",
    )


def test_being_loaded_is_not_being_held_by_a_terminal() -> None:
    """The daemon never unloads a thread, so `loaded` alone proves nothing."""
    assert threads.resolve(created_here=False, loaded=True, terminal_holds=False) == (
        "terminal",
        "none",
    )


def test_a_thread_whose_terminal_left_stays_ours_to_drive() -> None:
    gone = {"created_here": False, "loaded": True, "terminal_holds": False}
    assert threads.resolve(**gone) == ("terminal", "none")
    assert threads.resolve(**gone, local_turn=True) == ("terminal", "remote")
    # A thread this device started never depended on a terminal being there.
    assert threads.resolve(created_here=True, loaded=True, terminal_holds=False) == (
        "remote",
        "remote",
    )


def test_a_thread_with_nothing_in_it_is_not_a_session_yet() -> None:
    empty = threads.ThreadSummary.parse(thread_row("t-new", preview="", name=None))
    assert empty is not None and threads.is_empty(empty) is True
    used = threads.ThreadSummary.parse(thread_row("t-new"))
    assert used is not None and threads.is_empty(used) is False
    named = threads.ThreadSummary.parse(thread_row("t-new", preview="", name="Typecheck"))
    assert named is not None and threads.is_empty(named) is False


def test_a_codex_on_a_terminal_is_a_tui_whatever_flags_it_carries() -> None:
    def proc(command: str, has_tty: bool = True) -> Proc:
        return Proc(pid=7, ppid=1, start="s", command=command, has_tty=has_tty)

    assert terminals.looks_like_a_tui(proc("/Users/me/.local/bin/codex")) is True
    assert terminals.looks_like_a_tui(proc("codex resume 01a08bde")) is True
    # Flags decide nothing: on Codex 0.154 all of these join the shared daemon.
    bypass = "codex --dangerously-bypass-approvals-and-sandbox"
    assert terminals.looks_like_a_tui(proc(bypass)) is True
    assert terminals.looks_like_a_tui(proc("codex -c model_reasoning_effort=low")) is True
    assert terminals.looks_like_a_tui(proc("codex --enable hooks")) is True
    # No terminal, or a helper subcommand rather than a TUI at all.
    assert terminals.looks_like_a_tui(proc("codex", has_tty=False)) is False
    assert terminals.looks_like_a_tui(proc("codex app-server --listen unix://")) is False
    assert terminals.looks_like_a_tui(proc("/App/codex -c features.host=true app-server")) is False
    assert terminals.looks_like_a_tui(proc("codex exec review the diff")) is False
    assert terminals.looks_like_a_tui(proc("/usr/bin/python -m http.server")) is False


async def scan_with(
    monkeypatch: pytest.MonkeyPatch,
    procs: list[Proc],
    opened: tuple[dict[int, OpenPaths], bool],
) -> terminals.TerminalScan:
    """Run `scan_terminals` over a fixed process list and a fixed `lsof` answer."""

    async def fake_processes() -> Scan:
        return Scan(procs=procs, complete=True)

    async def fake_open(pids: list[int]) -> tuple[dict[int, OpenPaths], bool]:
        return opened

    monkeypatch.setattr(terminals, "scan_processes", fake_processes)
    monkeypatch.setattr(terminals, "process_open_paths", fake_open)
    return await terminals.scan_terminals()


async def test_a_tui_holding_its_own_rollout_is_not_the_daemons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only way to tell an embedded app-server from a shared one.

    Observed: a TUI started with
    `--dangerously-bypass-approvals-and-sandbox` held no rollout, the shared
    daemon held it, and the daemon relayed the turns typed into that TUI.
    """
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(terminals, "SESSIONS_DIR", sessions)
    repo = os.path.realpath(str(tmp_path / "repo"))
    procs = [
        Proc(pid=11, ppid=1, start="s", command="codex --dangerously-bypass-approvals-and-sandbox"),
        Proc(pid=12, ppid=1, start="s", command="codex"),
    ]
    held = {
        11: OpenPaths(cwd=repo, files=(str(tmp_path / "notes.md"),)),
        12: OpenPaths(cwd=repo, files=(str(sessions / "2026/09/12/rollout-t.jsonl"),)),
    }

    scan = await scan_with(monkeypatch, procs, (held, True))
    assert scan.complete is True
    assert scan.count(repo) == 1


async def test_a_scan_that_cannot_read_what_a_tui_holds_decides_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    procs = [Proc(pid=11, ppid=1, start="s", command="codex")]
    scan = await scan_with(monkeypatch, procs, ({}, False))
    assert scan.complete is False
    assert scan.count(os.path.realpath(str(tmp_path))) == 0


def test_a_scan_counts_the_terminals_in_each_directory(tmp_path: Path) -> None:
    scan = terminals.TerminalScan(cwds={os.path.realpath(str(tmp_path)): 2}, complete=True)
    assert scan.holds(str(tmp_path)) is True
    assert scan.count(str(tmp_path)) == 2
    assert scan.count(str(tmp_path / "sub")) == 0
    assert scan.holds("") is False


def test_a_title_generation_thread_is_never_a_session() -> None:
    rows = [thread_row(), thread_row("ephemeral-1", ephemeral=True)]
    assert [item.thread_id for item in threads.summaries(rows)] == [THREAD]


def test_an_active_status_is_recognised() -> None:
    assert threads.is_active({"type": "active", "activeFlags": []}) is True
    assert threads.is_active({"type": "idle"}) is False


# ------------------------------------------------------------------ indexing


@pytest.mark.parametrize("source", ["cli", "vscode"])
async def test_history_becomes_resumable_sessions_and_loaded_ones_become_shared(
    harness: Harness,
    source: str,
) -> None:
    await started(harness, loaded=[], row=thread_row(originator="codex-tui", source=source))
    assert harness.hub.entry(THREAD).session.control == "none"
    assert harness.hub.entry(THREAD).session.origin == "terminal"

    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD]}
    await harness.service.refresh()
    entry = harness.hub.entry(THREAD)
    assert entry.session.control == "shared"
    assert isinstance(entry.runner, CodexDaemonSession)
    assert harness.daemon.sent("thread/resume")[-1]["excludeTurns"] is True


@pytest.mark.parametrize(
    ("originator", "source"),
    [(provenance.DAEMON_CLIENT_NAME, "vscode"), ("codex-tui", "cli"), ("codex-tui", "vscode")],
)
async def test_a_thread_started_elsewhere_becomes_a_shared_session(
    harness: Harness, originator: str, source: str
) -> None:
    await started(harness, loaded=[])
    await harness.daemon.notify(
        "thread/started", {"thread": thread_row("t-new", originator=originator, source=source)}
    )
    await settle(
        lambda: (
            "t-new" in harness.hub.entries
            and harness.hub.entry("t-new").session.control == "shared"
        )
    )
    assert harness.hub.entry("t-new").session.control == "shared"
    assert harness.hub.entry("t-new").session.origin == "terminal"


@pytest.mark.parametrize("source", ["cli", "vscode"])
async def test_a_loaded_terminal_thread_missing_from_history_is_adopted_by_read(
    harness: Harness, source: str
) -> None:
    harness.daemon.replies["thread/read"] = {
        "thread": thread_row("t-tui", originator="codex-tui", source=source)
    }
    await started(harness, loaded=["t-tui"])
    entry = harness.hub.entry("t-tui")
    assert (entry.session.origin, entry.session.control) == ("terminal", "shared")
    assert isinstance(entry.runner, CodexDaemonSession)
    assert harness.daemon.sent("thread/read") == [{"threadId": "t-tui"}]
    assert harness.daemon.sent("thread/resume")[-1]["threadId"] == "t-tui"


async def test_a_terminal_opening_an_archived_thread_takes_it_out_of_the_archive(
    harness: Harness,
) -> None:
    """Amendment A15: a TUI sitting in a thread is the session coming back to life."""
    harness.registry.upsert_session(
        Session(
            session_id="t-new",
            device_id="dev-1",
            agent="codex",
            cwd="/repo",
            state="idle",
            origin="terminal",
            control="none",
            archived=True,
        )
    )
    harness.hub.load()
    assert harness.hub.entry("t-new").session.state == "stopped"

    await started(harness, loaded=[])
    await harness.daemon.notify("thread/started", {"thread": thread_row("t-new")})
    await settle(lambda: harness.hub.entry("t-new").session.archived is False)
    assert harness.hub.entry("t-new").session.state != "stopped"


async def test_one_terminal_claims_one_thread_in_its_directory(harness: Harness) -> None:
    """The duplicate-session bug, in the shape the real daemon produces it.

    Codex never unloads a thread, so every `codex` ever run in a directory is
    still loaded there. One live TUI must therefore speak for one thread, not
    for the whole directory.
    """
    await started(harness, loaded=[])
    older = thread_row("t-old", updatedAt=1788946000)
    recent = thread_row("t-recent", updatedAt=1788946900)
    harness.daemon.replies["thread/list"] = {"data": [thread_row(), older, recent]}
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, "t-old", "t-recent"]}
    await harness.service.refresh()

    control = {name: harness.hub.entry(name).session.control for name in (THREAD, "t-old")}
    assert control == {THREAD: "none", "t-old": "none"}
    assert harness.hub.entry("t-recent").session.control == "shared"
    # The claim is sticky: the next scan does not move it to another thread.
    await harness.service.refresh()
    assert harness.hub.entry("t-recent").session.control == "shared"


async def test_two_terminals_in_one_directory_claim_two_threads(harness: Harness) -> None:
    await started(harness, loaded=[])
    harness.terminals.counts = {"/repo": 2}
    harness.daemon.replies["thread/list"] = {
        "data": [thread_row(), thread_row("t-old", updatedAt=1788946000)]
    }
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, "t-old"]}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.control == "shared"
    assert harness.hub.entry("t-old").session.control == "shared"


async def test_a_thread_a_terminal_opens_is_no_session_until_it_speaks(
    harness: Harness,
) -> None:
    """The sequence a bare `codex` really produces, recorded from a real run.

    The TUI opens a thread as it starts, minutes before anything is typed into
    it, and that thread has no name, no preview and no rollout to resume.
    """
    await started(harness, loaded=[])
    harness.daemon.replies["thread/list"] = {
        "data": [thread_row(), thread_row("t-old", updatedAt=1788946000)]
    }
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, "t-old"]}
    await harness.service.refresh()

    opened = thread_row("t-tui", preview="", updatedAt=1788947000)
    await harness.daemon.notify("thread/started", {"thread": opened})
    await asyncio.sleep(0.1)
    assert "t-tui" not in harness.hub.entries
    assert harness.service.knows("t-tui") is True

    used = thread_row("t-tui", preview="ok then", updatedAt=1788947000)
    harness.daemon.replies["thread/read"] = {"thread": used}
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": "t-tui", "status": {"type": "active"}}
    )
    # Registration precedes asynchronous attachment and the active-status notification.
    await settle(
        lambda: (
            "t-tui" in harness.hub.entries
            and harness.hub.entry("t-tui").session.control == "shared"
            and harness.hub.entry("t-tui").session.state == "running"
        )
    )
    assert harness.hub.entry("t-tui").session.control == "shared"
    assert harness.hub.entry("t-tui").session.state == "running"
    assert harness.hub.entry("t-old").session.control == "none"

    # The one terminal in the directory now speaks for the thread it opened.
    harness.daemon.replies["thread/list"] = {
        "data": [thread_row(), thread_row("t-old", updatedAt=1788946000), used]
    }
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, "t-old", "t-tui"]}
    await harness.service.refresh()
    control = {name: harness.hub.entry(name).session.control for name in (THREAD, "t-old")}
    assert control == {THREAD: "none", "t-old": "none"}
    assert harness.hub.entry("t-tui").session.control == "shared"


async def test_a_terminal_opened_and_abandoned_never_becomes_a_session(
    harness: Harness,
) -> None:
    """A TUI closed before its first message leaves its thread loaded for good."""
    await started(harness, loaded=[])
    await harness.daemon.notify("thread/started", {"thread": thread_row("t-empty", preview="")})
    await asyncio.sleep(0.1)
    assert "t-empty" not in harness.hub.entries

    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD, "t-empty"]}
    harness.daemon.replies["thread/read"] = {"thread": thread_row("t-empty", preview="")}
    await harness.service.refresh()
    assert "t-empty" not in harness.hub.entries
    assert harness.hub.entry(THREAD).session.control == "shared"


async def test_an_empty_thread_found_on_a_cold_start_is_left_alone(harness: Harness) -> None:
    """Nothing distinguishes a thread opened before we connected from one opened now."""
    harness.daemon.replies["thread/read"] = {"thread": thread_row("t-empty", preview="")}
    await started(harness, loaded=[THREAD, "t-empty"])
    assert "t-empty" not in harness.hub.entries
    assert harness.service.knows("t-empty") is True


async def test_a_thread_deleted_while_we_are_attached_is_forgotten(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    assert isinstance(harness.hub.entry(THREAD).runner, CodexDaemonSession)

    harness.daemon.replies["thread/list"] = {"data": [thread_row("t-other", updatedAt=1788946000)]}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    await harness.service.refresh()
    assert THREAD not in harness.hub.entries
    assert [frame for frame in harness.frames if frame.get("type") == "session.removed"]


async def test_an_ephemeral_thread_start_is_ignored(harness: Harness) -> None:
    await started(harness, loaded=[])
    await harness.daemon.notify("thread/started", {"thread": thread_row("t-title", ephemeral=True)})
    await asyncio.sleep(0.1)
    assert "t-title" not in harness.hub.entries


async def test_closing_a_thread_hands_the_session_back(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.notify("thread/closed", {"threadId": THREAD})
    await settle(lambda: harness.hub.entry(THREAD).runner is None)
    assert harness.hub.entry(THREAD).session.control == "none"


async def test_a_message_typed_in_the_terminal_makes_a_remote_thread_shared(
    harness: Harness,
) -> None:
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    entry.session.origin = "remote"
    # The scan hands no claim to a thread this device started, TUI or no TUI.
    await harness.service.refresh_terminals()
    assert entry.session.control == "remote"

    await harness.daemon.notify(
        "item/completed",
        {
            "threadId": THREAD,
            "item": {
                "id": "u1",
                "type": "userMessage",
                "clientId": "61d36cea",
                "content": [{"text": "hello"}],
            },
        },
    )
    await settle(lambda: harness.hub.entry(THREAD).session.control == "shared")
    assert [event["source"] for event in harness.events("user_message")] == ["terminal"]


# ------------------------------------------------------------ terminal exits


async def test_a_tui_that_exits_hands_an_idle_thread_back(harness: Harness) -> None:
    """The daemon says nothing when a TUI leaves; the missing process is the news."""
    await started(harness, loaded=[THREAD])
    assert harness.hub.entry(THREAD).session.control == "shared"

    harness.terminals.cwds = set()
    await harness.service.refresh()

    entry = harness.hub.entry(THREAD)
    assert entry.session.control == "none"
    assert entry.session.origin == "terminal"
    # The subscription stays: the thread is still loaded and still ours to read.
    assert isinstance(entry.runner, CodexDaemonSession)
    assert harness.events("meta")[-1]["control"] == "none"


async def test_a_tui_that_exits_mid_turn_leaves_our_own_turn_running(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})

    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "remote"

    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.control == "none")


async def test_a_terminal_that_types_again_takes_the_thread_back(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "none"

    await harness.daemon.echo_prompt(THREAD, "back at my desk", item_id="u9", client_id="tui-7")
    await settle(lambda: harness.hub.entry(THREAD).session.control == "shared")


async def test_a_terminal_that_just_typed_outranks_the_next_scan(harness: Harness) -> None:
    """A TUI resumed from another directory is invisible to the scan, not gone."""
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    await harness.daemon.echo_prompt(THREAD, "still here", item_id="u9", client_id="tui-7")
    await settle(lambda: len(harness.bubbles()) == 1)

    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "shared"
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "none"


async def test_an_incomplete_scan_never_hands_a_session_over(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    harness.terminals.complete = False
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "shared"


async def test_a_terminal_that_comes_back_makes_the_thread_shared_again(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "none"

    harness.terminals.cwds = {"/repo"}
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "shared"


async def test_nothing_is_scanned_while_no_thread_is_loaded(harness: Harness) -> None:
    await started(harness, loaded=[])
    scans = harness.terminals.scans
    await harness.service.refresh()
    assert harness.terminals.scans == scans


# -------------------------------------------------------------------- input


async def test_sending_while_idle_starts_a_turn_and_shows_one_bubble(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    result = await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})

    assert result == {"accepted": "sent"}
    start = harness.daemon.sent("turn/start")[-1]
    assert start["input"] == [{"type": "text", "text": "go"}]
    messages = harness.events("user_message")
    assert len(messages) == 1
    assert messages[0]["delivery"] == "delivered"


async def test_our_own_prompt_is_not_echoed_twice(harness: Harness) -> None:
    """The daemon replays the prompt on `item/started` and again on `item/completed`."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    await harness.daemon.echo_prompt(THREAD, "go", item_id="u1")
    await asyncio.sleep(0.1)
    assert list(harness.bubbles().values()) == ["remote"]


async def test_a_stamped_echo_of_our_own_prompt_is_still_ours(harness: Harness) -> None:
    """A `clientId` on our own echo must not read as a terminal sharing the thread."""
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    entry.session.origin = "remote"
    await harness.service.refresh_terminals()
    harness.daemon.echo_client_id = "dev-99"

    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    await harness.daemon.echo_prompt(THREAD, "go", item_id="u1")
    await asyncio.sleep(0.1)
    assert list(harness.bubbles().values()) == ["remote"]
    assert harness.hub.entry(THREAD).session.control == "remote"

    # Another client id on a message we never sent is the terminal typing.
    await harness.daemon.echo_prompt(THREAD, "and now this", item_id="u2", client_id="tui-7")
    await settle(lambda: harness.hub.entry(THREAD).session.control == "shared")
    assert list(harness.bubbles().values()) == ["remote", "terminal"]


async def test_only_the_first_match_consumes_an_echo(harness: Harness) -> None:
    """A terminal user typing the same words still gets their own bubble."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    await harness.daemon.echo_prompt(THREAD, "go", item_id="u1")
    await harness.daemon.echo_prompt(THREAD, "go", item_id="u2")
    await settle(lambda: len(harness.bubbles()) == 2)
    assert list(harness.bubbles().values()) == ["remote", "terminal"]


async def test_a_backfilled_echo_is_not_republished(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    await harness.daemon.echo_prompt(THREAD, "go", item_id="u1")
    await asyncio.sleep(0.1)
    harness.daemon.replies["thread/items/list"] = {
        "data": [
            {
                "type": "userMessage",
                "id": "u1",
                "clientId": None,
                "content": [{"type": "text", "text": "go"}],
            }
        ]
    }
    runner = harness.hub.entry(THREAD).runner
    assert isinstance(runner, CodexDaemonSession)
    await runner.backfill()
    assert list(harness.bubbles().values()) == ["remote"]


async def test_sending_during_a_turn_steers_it(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    result = await harness.hub.send({"id": "req-2", "session_id": THREAD, "text": "actually stop"})

    assert result == {"accepted": "steered"}
    steer = harness.daemon.sent("turn/steer")[-1]
    assert steer["expectedTurnId"] == "turn-1"


async def test_an_explicit_queue_holds_the_message_until_the_turn_ends(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    result = await harness.hub.send(
        {"id": "req-2", "session_id": THREAD, "text": "then this", "mode": "queue"}
    )
    assert result == {"accepted": "queued", "queued_id": "req-2"}
    assert harness.events("queue")[-1]["pending"][0]["text"] == "then this"

    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await settle(lambda: len(harness.daemon.sent("turn/start")) == 2)
    assert harness.daemon.sent("turn/start")[-1]["input"][0]["text"] == "then this"


async def test_stopping_interrupts_the_turn_whoever_started_it(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.notify("turn/started", {"threadId": THREAD, "turn": {"id": "turn-9"}})
    await settle(lambda: harness.hub.entry(THREAD).session.turn is not None)

    async def stop() -> None:
        await harness.hub.stop({"session_id": THREAD})

    task = asyncio.create_task(stop())
    interrupt = await harness.daemon.wait_for_call("turn/interrupt")
    assert interrupt == {"threadId": THREAD, "turnId": "turn-9"}
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-9", "status": "interrupted"}}
    )
    await task
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"


async def test_settings_reach_the_daemon_because_they_are_shared(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.set_options(
        {"session_id": THREAD, "permission_mode": "on-request", "effort": "low"}
    )
    update = harness.daemon.sent("thread/settings/update")[-1]
    assert update["threadId"] == THREAD
    assert update["approvalPolicy"] == "on-request"
    assert update["effort"] == "low"


async def test_a_settings_change_from_the_terminal_is_mirrored(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.notify(
        "thread/settings/updated",
        {"threadId": THREAD, "threadSettings": {"effort": "low", "model": "gpt-5.4-codex"}},
    )
    await settle(lambda: harness.hub.entry(THREAD).session.effort == "low")


async def test_takeover_is_a_conflict_on_a_shared_session(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    with pytest.raises(RcError) as caught:
        await harness.hub.takeover({"session_id": THREAD})
    assert caught.value.code == "conflict"
    assert caught.value.message == "already attached"


async def test_images_travel_as_inputs_and_other_files_are_named_in_the_prompt(
    harness: Harness,
) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.send(
        {
            "id": "req-1",
            "session_id": THREAD,
            "text": "look",
            "attachments": [
                {"name": "shot.png", "mime": "image/png", "data_base64": "aGk="},
                {"name": "notes.txt", "mime": "text/plain", "data_base64": "aGk="},
            ],
        }
    )
    inputs = harness.daemon.sent("turn/start")[-1]["input"]
    assert inputs[0]["type"] == "text"
    assert "notes.txt" in inputs[0]["text"]
    assert inputs[1]["type"] == "localImage"
    assert inputs[1]["path"].endswith("shot.png")


# ---------------------------------------------------------------- approvals


async def test_an_approval_offers_the_options_the_daemon_lists(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.ask(
        11,
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "exec-1",
            "command": "/bin/zsh -lc 'touch approved.txt'",
            "cwd": "/repo",
            "commandActions": [{"type": "unknown", "command": "touch approved.txt"}],
            "availableDecisions": [
                "accept",
                {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch"]}},
                "cancel",
            ],
        },
    )
    await settle(lambda: harness.events("approval"))
    card = harness.events("approval")[0]
    assert [option["id"] for option in card["options"]] == ["allow", "allow_always", "deny"]
    assert card["input"]["command_actions"][0]["command"] == "touch approved.txt"
    assert card["tool_kind"] == "shell"

    await harness.hub.approve(
        {"session_id": THREAD, "request_id": card["request_id"], "option_id": "allow"}
    )
    answer = await asyncio.wait_for(harness.daemon.answers.get(), timeout=2)
    assert answer["result"] == {"decision": "accept"}
    assert harness.events("approval")[-1]["decision"] == {"option_id": "allow", "by": "remote"}


async def test_the_amendment_option_carries_its_payload_back(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    amendment = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch", "a.txt"]}}
    await harness.daemon.ask(
        12,
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "exec-2",
            "command": "touch a.txt",
            "availableDecisions": ["accept", amendment, "cancel"],
        },
    )
    await settle(lambda: harness.events("approval"))
    card = harness.events("approval")[0]
    await harness.hub.approve(
        {"session_id": THREAD, "request_id": card["request_id"], "option_id": "allow_always"}
    )
    answer = await asyncio.wait_for(harness.daemon.answers.get(), timeout=2)
    assert answer["result"] == {"decision": amendment}


async def test_a_prompt_answered_in_the_terminal_resolves_as_elsewhere(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.ask(
        13,
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "exec-3",
            "command": "touch b.txt",
            "availableDecisions": ["accept", "cancel"],
        },
    )
    await settle(lambda: harness.events("approval"))
    await harness.daemon.notify("serverRequest/resolved", {"threadId": THREAD, "requestId": 13})
    await settle(lambda: len(harness.events("approval")) >= 2)
    last = harness.events("approval")[-1]
    assert last["status"] == "resolved"
    assert last["decision"] == {"option_id": "elsewhere", "by": "terminal"}
    answer = await asyncio.wait_for(harness.daemon.answers.get(), timeout=2)
    assert answer["result"] == {}


async def test_a_question_is_answered_with_the_option_labels(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.ask(
        14,
        "item/tool/requestUserInput",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "ask-1",
            "isBlocking": True,
            "questions": [
                {
                    "id": "q1",
                    "question": "Which one?",
                    "options": [{"label": "Left"}, {"label": "Right"}],
                }
            ],
        },
    )
    await settle(lambda: harness.events("question"))
    card = harness.events("question")[0]
    await harness.hub.answer(
        {"session_id": THREAD, "request_id": card["request_id"], "answers": {"q1": "o1"}}
    )
    answer = await asyncio.wait_for(harness.daemon.answers.get(), timeout=2)
    assert answer["result"] == {"answers": {"q1": {"answers": ["Right"]}}}


def test_option_mapping_covers_the_amendment_vocabulary() -> None:
    built = approvals.build_options(["accept", "acceptForSession", "cancel"], ())
    assert [option["id"] for option in built.options] == ["allow", "allow_session", "deny"]
    assert [option["style"] for option in built.options] == ["primary", "secondary", "danger"]
    assert built.decisions["deny"] == "cancel"


def test_decline_is_dropped_when_cancel_is_also_offered() -> None:
    built = approvals.build_options(["accept", "decline", "cancel"], ())
    assert [option["id"] for option in built.options] == ["allow", "deny"]
    assert built.decisions["deny"] == "cancel"


def test_decline_becomes_deny_when_it_is_the_only_refusal() -> None:
    built = approvals.build_options(["accept", "decline"], ())
    assert built.decisions["deny"] == "decline"


def test_a_prompt_with_no_decisions_falls_back_to_the_known_set() -> None:
    built = approvals.build_options(None, approvals.FILE_CHANGE_DECISIONS)
    assert [option["id"] for option in built.options] == ["allow", "allow_session", "deny"]


def test_a_permission_grant_is_scoped_by_the_option_chosen() -> None:
    params = {"permissions": {"fileSystem": {"read": ["/repo"]}}}
    assert approvals.permission_reply("allow", params)["scope"] == "turn"
    assert approvals.permission_reply("allow_session", params)["scope"] == "session"
    assert approvals.permission_reply("deny", params)["permissions"] == {}


# ---------------------------------------------------------------- fallback


async def test_the_hub_uses_the_spawned_app_server_when_the_daemon_is_absent(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(socket_dir / "absent.sock"))
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    registry = Registry(tmp_path / "state.sqlite3")
    try:
        hub = SessionHub(registry, publish, "dev-1", agents)
        service = CodexDaemonService(hub, "0.1.0")
        hub.codex_daemon = service
        assert await service.start("/bin/codex") is False
        assert service.ready is False
    finally:
        registry.close()


def test_a_thread_config_override_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RC_CODEX_THREAD_CONFIG", '{"features": {"hooks": false}}')
    assert thread_config() == {"features": {"hooks": False}}
    monkeypatch.setenv("RC_CODEX_THREAD_CONFIG", "not json")
    assert thread_config() is None
    monkeypatch.delenv("RC_CODEX_THREAD_CONFIG")
    assert thread_config() is None


# ------------------------------------------------------- reconnect/backfill


async def test_a_reconnect_re_resumes_and_backfills_from_the_last_item(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.daemon.notify(
        "item/completed",
        {
            "threadId": THREAD,
            "item": {"id": "a1", "type": "agentMessage", "text": "first"},
        },
    )
    await settle(lambda: harness.events("assistant_text"))
    harness.daemon.replies["thread/items/list"] = {
        "data": [
            {"id": "a2", "type": "agentMessage", "text": "missed"},
            {"id": "a1", "type": "agentMessage", "text": "first"},
        ]
    }
    resumes = len(harness.daemon.sent("thread/resume"))

    await harness.daemon.drop()
    await settle(lambda: len(harness.daemon.sent("thread/resume")) > resumes, timeout=8)
    await settle(
        lambda: (
            [event["text"] for event in harness.events("assistant_text")] == ["first", "missed"]
        ),
        timeout=5,
    )


async def test_a_daemon_bootstrapped_later_switches_the_mode_without_a_restart(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment A11: the device re-probes the socket on the mirror's scan interval."""
    socket = socket_dir / "late.sock"
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(socket))
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    switches: list[bool] = []

    async def on_mode_change() -> None:
        switches.append(True)

    registry = Registry(tmp_path / "state.sqlite3")
    server = FakeDaemon(socket)
    try:
        hub = SessionHub(registry, publish, "dev-1", agents)
        service = CodexDaemonService(hub, "0.1.0", on_mode_change)
        hub.codex_daemon = service
        await service.tick("/bin/codex")
        before = service.ready
        assert (before, switches) == (False, [])

        await server.start()
        server.replies["thread/list"] = {"data": [thread_row()]}
        server.replies["thread/loaded/list"] = {"data": []}
        await service.tick("/bin/codex")
        after = service.ready
        assert (after, switches) == (True, [True])
        assert THREAD in hub.entries
    finally:
        await service.stop()
        await server.stop()
        registry.close()


async def test_an_option_the_block_never_offered_is_a_bad_request(harness: Harness) -> None:
    """PROTOCOL 6.3: `elsewhere` and anything else unoffered is rejected, not obeyed."""
    await started(harness, loaded=[THREAD])
    await harness.daemon.ask(
        21,
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "exec-9",
            "command": "touch c.txt",
            "availableDecisions": ["accept", "cancel"],
        },
    )
    await settle(lambda: harness.events("approval"))
    card = harness.events("approval")[0]
    for option_id in ("elsewhere", "allow_session", "nonsense"):
        with pytest.raises(RcError) as caught:
            await harness.hub.approve(
                {
                    "session_id": THREAD,
                    "request_id": card["request_id"],
                    "option_id": option_id,
                }
            )
        assert caught.value.code == "bad_request"
    await harness.hub.approve(
        {"session_id": THREAD, "request_id": card["request_id"], "option_id": "deny"}
    )
    answer = await asyncio.wait_for(harness.daemon.answers.get(), timeout=2)
    assert answer["result"] == {"decision": "cancel"}


async def test_interrupt_mode_stops_the_turn_and_then_sends(harness: Harness) -> None:
    """A11 clarification: `mode` keeps the protocol vocabulary on a shared thread."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})

    async def send_interrupting() -> dict[str, Any]:
        return await harness.hub.send(
            {
                "id": "req-2",
                "session_id": THREAD,
                "text": "no, this instead",
                "mode": "interrupt",
            }
        )

    task = asyncio.create_task(send_interrupting())
    await harness.daemon.wait_for_call("turn/interrupt")
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "interrupted"}}
    )
    assert await task == {"accepted": "sent"}
    assert harness.daemon.sent("turn/start")[-1]["input"][0]["text"] == "no, this instead"
    assert harness.events("turn_completed")[0]["stop_reason"] == "interrupted"


async def test_a_thread_deleted_in_codex_stops_being_a_session(harness: Harness) -> None:
    """`codex delete` removes the thread, so the device must not keep a ghost of it."""
    other = "01a08c99-0000-7000-8000-000000000001"
    # Used after the thread that stays, so the page it was on would still show it.
    harness.daemon.replies["thread/list"] = {
        "data": [thread_row(other, updatedAt=1788946300), thread_row()]
    }
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    harness.daemon.replies["thread/items/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True
    assert {THREAD, other} <= set(harness.hub.entries)

    harness.daemon.replies["thread/list"] = {"data": [thread_row()]}
    await harness.service.refresh()

    assert other not in harness.hub.entries
    assert THREAD in harness.hub.entries
    removed = [f for f in harness.frames if f.get("type") == "session.removed"]
    assert [f["session_id"] for f in removed] == [other]
    assert harness.service.knows(other) is False


async def test_an_older_thread_that_fell_off_the_page_is_kept(harness: Harness) -> None:
    """With more behind the page, absence only proves deletion above the cutoff."""
    old = "01a08c99-0000-7000-8000-000000000002"
    harness.daemon.replies["thread/list"] = {
        "data": [thread_row(), thread_row(old, updatedAt=1788000000)]
    }
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    harness.daemon.replies["thread/items/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True

    harness.daemon.replies["thread/list"] = {"data": [thread_row()], "nextCursor": "older"}
    await harness.service.refresh()

    assert old in harness.hub.entries
    assert [f for f in harness.frames if f.get("type") == "session.removed"] == []


async def test_a_loaded_thread_is_never_forgotten(harness: Harness) -> None:
    """A thread we drive has no rollout to list yet; absence there means nothing."""
    await started(harness, loaded=[THREAD])
    harness.daemon.replies["thread/list"] = {"data": []}
    await harness.service.refresh()
    assert THREAD in harness.hub.entries
    assert [f for f in harness.frames if f.get("type") == "session.removed"] == []


# ------------------------------------------- work another application owns


def desktop_row(thread_id: str) -> dict[str, Any]:
    """What the ChatGPT desktop app's chats and automations look like in the index."""
    return thread_row(thread_id, originator="Codex Desktop", source="vscode")


def seed(harness: Harness, session_id: str, origin: str = "terminal") -> SessionEntry:
    """A Codex session stored before A18, with one event behind it."""
    entry = harness.hub.register_mirrored(
        Session(
            session_id=session_id,
            device_id="dev-1",
            agent="codex",
            cwd="/repo",
            title="Nightly digest",
            state="idle",
            origin=origin,  # type: ignore[arg-type]
            control="none",
        )
    )
    entry.channel.start()
    return entry


def test_the_index_keeps_only_the_threads_this_device_may_show() -> None:
    page = [
        thread_row("t-ours"),
        thread_row("t-tui", originator="codex-tui", source="cli"),
        thread_row("t-app-server-tui", originator="codex-tui", source="vscode"),
        desktop_row("t-desktop"),
        thread_row("t-ide", originator="vscode-extension", source="vscode"),
        thread_row("t-sub", originator="codex-tui", source={"subAgent": {"other": "guardian"}}),
    ]
    assert [summary.thread_id for summary in threads.summaries(page)] == [
        "t-ours",
        "t-tui",
        "t-app-server-tui",
    ]


async def test_a_thread_another_application_owns_is_never_adopted(harness: Harness) -> None:
    """`thread/read` is the other way in, and it answers to the same rule."""
    foreign = "01a09321-d428-7742-b144-2e5b90421371"
    harness.daemon.replies["thread/list"] = {"data": []}
    harness.daemon.replies["thread/loaded/list"] = {"data": [foreign]}
    harness.daemon.replies["thread/read"] = {"thread": desktop_row(foreign)}
    harness.daemon.replies["thread/items/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True
    assert foreign not in harness.hub.entries

    # A foreign thread that speaks is still foreign, and the answer stands: a
    # thread being used elsewhere must not cost a `thread/read` per event.
    await harness.daemon.notify(
        "item/started", {"threadId": foreign, "item": {"id": "i1", "type": "agentMessage"}}
    )
    await asyncio.sleep(0.1)
    await harness.service.refresh()
    assert foreign not in harness.hub.entries
    assert harness.service.knows(foreign) is False
    assert [method for method, _ in harness.daemon.calls].count("thread/read") == 1


async def test_a_thread_another_application_opens_is_not_a_session(harness: Harness) -> None:
    await started(harness, loaded=[])
    await harness.daemon.notify("thread/started", {"thread": desktop_row("t-desktop")})
    await asyncio.sleep(0.1)
    assert "t-desktop" not in harness.hub.entries


@pytest.mark.parametrize("source", ["cli", "vscode"])
async def test_threads_published_before_the_rule_are_withdrawn_at_startup(
    harness: Harness,
    source: str,
) -> None:
    foreign, ours, silent, terminal = "t-desktop", "t-remote", "t-silent", "t-tui"
    for session_id, origin in (
        (foreign, "terminal"),
        (ours, "remote"),
        (silent, "terminal"),
        (terminal, "terminal"),
    ):
        entry = seed(harness, session_id, origin)
        await entry.channel.emit("user_message", block_id="b1", text="x", source="terminal")

    def answer(method: str, params: dict[str, Any]) -> Any:
        if method != "thread/read":
            return None
        # Nothing at all for `t-silent`: a daemon that cannot describe a thread
        # has said nothing about whose it is.
        if params.get("threadId") == foreign:
            return {"thread": desktop_row(foreign)}
        if params.get("threadId") == terminal:
            return {"thread": thread_row(terminal, originator="codex-tui", source=source)}
        return {}

    harness.daemon.responder = answer
    harness.daemon.replies["thread/list"] = {"data": []}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True

    assert sorted(harness.hub.entries) == [ours, silent, terminal]
    removed = [f["session_id"] for f in harness.frames if f.get("type") == "session.removed"]
    assert removed == [foreign]
    assert harness.registry.has_events(foreign) is False
    assert harness.registry.has_events(silent) is True
    assert harness.registry.has_events(terminal) is True
    # A session this device drives is never read about at all.
    assert [params for method, params in harness.daemon.calls if method == "thread/read"] == [
        {"threadId": foreign},
        {"threadId": silent},
        {"threadId": terminal},
    ]


async def test_a_withdrawal_the_link_missed_is_repeated_on_the_next_link(
    harness: Harness,
) -> None:
    """The gateway keeps every session a device announced, so the frame is said again."""
    foreign = "t-desktop"
    seed(harness, foreign)
    harness.daemon.responder = lambda method, params: (
        {"thread": desktop_row(foreign)} if method == "thread/read" else None
    )
    harness.daemon.replies["thread/list"] = {"data": []}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    harness.hub.link_up = lambda: False
    assert await harness.service.start("/bin/codex") is True

    def removals() -> list[str]:
        return [f["session_id"] for f in harness.frames if f.get("type") == "session.removed"]

    assert removals() == [foreign]

    harness.hub.link_up = lambda: True
    harness.hub.snapshot()  # the `hello` of the link that came back
    await harness.hub.sweep_ghosts()
    assert removals() == [foreign, foreign]


# ------------------------------------------------------------------- titles


async def test_the_name_codex_gives_a_thread_becomes_the_session_title(
    harness: Harness,
) -> None:
    await started(harness, loaded=[THREAD])
    assert harness.hub.entry(THREAD).session.title == "Typecheck the web app"

    await harness.daemon.notify(
        "thread/name/updated", {"threadId": THREAD, "threadName": "Fix the web typecheck"}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.title == "Fix the web typecheck")
    assert harness.events("meta")[-1]["title"] == "Fix the web typecheck"
    updates = [
        frame["session"]["title"]
        for frame in harness.frames
        if frame.get("type") == "session.updated"
    ]
    assert updates[-1] == "Fix the web typecheck"


async def test_a_name_codex_announces_replaces_a_title_an_app_set(harness: Harness) -> None:
    """A55: Codex's own record is the truth, and a rename is not activity."""
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    entry.session.updated_at = 1_000
    await titles.from_user(entry.channel, "Ledger migration")

    await harness.daemon.notify(
        "thread/name/updated", {"threadId": THREAD, "threadName": "Fix the web typecheck"}
    )
    await settle(lambda: entry.session.title == "Fix the web typecheck")
    assert entry.session.updated_at == 1_000


async def test_a_named_thread_in_history_is_titled_by_its_name(harness: Harness) -> None:
    harness.daemon.replies["thread/list"] = {"data": [thread_row(name="Fix the web typecheck")]}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True
    assert harness.hub.entry(THREAD).session.title == "Fix the web typecheck"


def test_a_thread_summary_separates_its_name_from_its_first_prompt() -> None:
    assert threads.ThreadSummary.parse(thread_row()).name == ""  # type: ignore[union-attr]
    named = threads.ThreadSummary.parse(thread_row(name="Fix the web typecheck"))
    assert named is not None
    assert (named.name, named.title) == ("Fix the web typecheck", "Fix the web typecheck")


# ------------------------------------- A12, and what a send does before it


SEND_REQUEST = "9a2f4c71-3e85-4d0b-b6a1-5c8e7d240f33"


async def test_a_send_carries_the_request_id_as_its_block(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": SEND_REQUEST, "session_id": THREAD, "text": "go"})
    assert [event["block_id"] for event in harness.events("user_message")] == [SEND_REQUEST]


async def test_a_send_publishes_the_message_before_it_asks_the_daemon_anything(
    harness: Harness,
) -> None:
    """Every request below the bubble is a round trip the sender waits through."""
    harness.daemon.errors["thread/resume"] = "no rollout found for thread id"
    await started(harness, loaded=[THREAD])
    await settle(lambda: bool(harness.daemon.sent("thread/resume")))
    harness.frames.clear()
    harness.daemon.calls.clear()

    await harness.hub.send({"id": SEND_REQUEST, "session_id": THREAD, "text": "go"})
    assert [method for method, _ in harness.daemon.calls] == ["turn/start", "thread/resume"]
    order = [
        frame["event"]["kind"] for frame in harness.frames if frame.get("type") == "session.event"
    ]
    assert order.index("user_message") < order.index("turn_started")


async def test_a_thread_that_cannot_be_resumed_is_not_asked_twice_per_send(
    harness: Harness,
) -> None:
    """A thread whose first turn has not run refuses every resume until it has."""
    harness.daemon.errors["thread/resume"] = "no rollout found for thread id"
    await started(harness, loaded=[THREAD])
    await settle(lambda: bool(harness.daemon.sent("thread/resume")))
    attempts = len(harness.daemon.sent("thread/resume"))

    await harness.hub.send({"id": SEND_REQUEST, "session_id": THREAD, "text": "go"})
    # One attempt for this send: the one after `turn/start`, when the rollout
    # the turn creates makes a resume possible for the first time.
    assert len(harness.daemon.sent("thread/resume")) == attempts + 1

    # And nothing about a notification that is not a turn boundary retries it.
    await harness.daemon.notify(
        "thread/name/updated", {"threadId": THREAD, "threadName": "Fix the typecheck"}
    )
    await harness.daemon.notify(
        "item/completed",
        {"threadId": THREAD, "item": {"id": "a1", "type": "agentMessage", "text": "ok"}},
    )
    await asyncio.sleep(0.1)
    assert len(harness.daemon.sent("thread/resume")) == attempts + 1


async def test_a_turn_boundary_makes_a_refused_resume_worth_another_try(
    harness: Harness,
) -> None:
    harness.daemon.errors["thread/resume"] = "no rollout found for thread id"
    await started(harness, loaded=[THREAD])
    await settle(lambda: bool(harness.daemon.sent("thread/resume")))
    attempts = len(harness.daemon.sent("thread/resume"))

    await harness.daemon.notify(
        "turn/started", {"threadId": THREAD, "turn": {"id": "turn-9", "status": "inProgress"}}
    )
    await settle(lambda: len(harness.daemon.sent("thread/resume")) > attempts)


# ------------------------------- A14: where a steered message lands in the log


STEER_REQUEST = "0d6b8e29-4a17-4c3f-9b52-8e1a6f70d3c4"


async def steering(harness: Harness) -> None:
    """A running turn this device started, with a message steered into it."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": SEND_REQUEST, "session_id": THREAD, "text": "first"})
    await harness.daemon.notify(
        "turn/started", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "inProgress"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.state == "running")
    result = await harness.hub.send(
        {"id": STEER_REQUEST, "session_id": THREAD, "text": "also this"}
    )
    assert result == {"accepted": "steered"}
    assert harness.daemon.sent("turn/steer")[-1]["expectedTurnId"] == "turn-1"


async def test_a_steered_message_is_published_when_the_agent_reads_it(harness: Harness) -> None:
    """Observed on the shared daemon: the echo arrives after the step in flight."""
    await steering(harness)
    # `accepted: "steered"` is immediate; the bubble is not.
    assert [event["block_id"] for event in harness.events("user_message")] == [SEND_REQUEST]

    await harness.daemon.notify(
        "item/completed",
        {"threadId": THREAD, "item": {"id": "a1", "type": "agentMessage", "text": "still going"}},
    )
    await settle(lambda: bool(harness.events("assistant_text")))
    assert [event["block_id"] for event in harness.events("user_message")] == [SEND_REQUEST]

    await harness.daemon.echo_prompt(THREAD, "also this", item_id="u-steer")
    await settle(lambda: len(harness.events("user_message")) == 2)
    steered = harness.events("user_message")[-1]
    assert steered["block_id"] == STEER_REQUEST
    assert (steered["source"], steered["delivery"], steered["text"]) == (
        "remote",
        "delivered",
        "also this",
    )
    # Which is the whole point: it sorts after what the agent was already saying.
    assert steered["first_seq"] > harness.events("assistant_text")[-1]["first_seq"]


async def test_a_steered_message_is_published_exactly_once(harness: Harness) -> None:
    """The second event for the item, a backfill and a reconnect all stay silent."""
    await steering(harness)
    await harness.daemon.echo_prompt(THREAD, "also this", item_id="u-steer")
    await settle(lambda: len(harness.events("user_message")) == 2)

    harness.daemon.replies["thread/items/list"] = {
        "data": [
            {
                "type": "userMessage",
                "id": "u-steer",
                "clientId": None,
                "content": [{"type": "text", "text": "also this"}],
            }
        ]
    }
    runner = harness.hub.entry(THREAD).runner
    assert isinstance(runner, CodexDaemonSession)
    await runner.backfill()
    await harness.daemon.echo_prompt(THREAD, "also this", item_id="u-steer")
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await settle(lambda: bool(harness.events("turn_completed")))
    assert [event["block_id"] for event in harness.events("user_message")] == [
        SEND_REQUEST,
        STEER_REQUEST,
    ]


async def test_a_steered_message_the_turn_never_read_lands_at_its_end(harness: Harness) -> None:
    await steering(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await settle(lambda: len(harness.events("user_message")) == 2)
    assert harness.events("user_message")[-1]["block_id"] == STEER_REQUEST
    # A turn that ran to the end read everything it was given, so no warning.
    assert harness.events("notice") == []
    # Inside the turn it was sent into, not after it.
    assert harness.events("user_message")[-1]["seq"] < harness.events("turn_completed")[-1]["seq"]


async def test_an_interrupted_turn_says_the_message_was_never_read(harness: Harness) -> None:
    await steering(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "interrupted"}}
    )
    await settle(lambda: len(harness.events("user_message")) == 2)
    assert harness.events("user_message")[-1]["block_id"] == STEER_REQUEST
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    notice = harness.events("notice")[-1]
    assert notice["level"] == "warn" and "read" in notice["text"]


# ------------------------------------------------------- making the daemon run


class RecordingRepair(DaemonRepair):
    """The repairs, recorded rather than run against a real Codex."""

    def __init__(self) -> None:
        super().__init__()
        self.ensured = 0
        self.checks: list[bool] = []

    async def ensure_running(self) -> bool:
        self.ensured += 1
        return False

    async def check_drift(self, safe: SafetyCheck) -> bool:
        self.checks.append(await safe())
        return False


async def test_the_scan_starts_the_daemon_before_it_tries_to_connect(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R1: without this nothing on the machine ever runs `daemon start`."""
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(socket_dir / "absent.sock"))

    async def publish(frame: dict[str, Any]) -> None:
        return None

    registry = Registry(tmp_path / "state.sqlite3")
    healer = RecordingRepair()
    try:
        hub = SessionHub(registry, publish, "dev-1", agents)
        service = CodexDaemonService(hub, "0.1.0", repair=healer)
        await service.tick("/bin/codex")
        assert (healer.ensured, healer.checks, service.ready) == (1, [], False)
    finally:
        await service.stop()
        registry.close()


async def test_a_connected_device_looks_for_a_version_drift_on_the_scan(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R3: `codex update` leaves the old app-server running, and says nothing."""
    socket = socket_dir / "drift.sock"
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(socket))

    async def publish(frame: dict[str, Any]) -> None:
        return None

    registry = Registry(tmp_path / "state.sqlite3")
    server = FakeDaemon(socket)
    await server.start()
    server.replies["thread/list"] = {"data": []}
    server.replies["thread/loaded/list"] = {"data": []}
    healer = RecordingRepair()
    quiet = FakeTerminals(set())
    try:
        hub = SessionHub(registry, publish, "dev-1", agents)
        service = CodexDaemonService(hub, "0.1.0", scan_terminals=quiet, repair=healer)
        await service.tick("/bin/codex")
        assert (service.ready, healer.ensured, healer.checks) == (True, 1, [])
        await service.tick("/bin/codex")
        assert healer.checks == [True]
    finally:
        await service.stop()
        await server.stop()
        registry.close()


async def test_a_restart_waits_for_the_terminals_the_daemon_is_serving(
    harness: Harness,
) -> None:
    await started(harness, loaded=[THREAD])
    # A TUI in the directory is a TUI a restart would take the app-server from.
    assert await harness.service._restart_safe() is False
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    assert await harness.service._restart_safe() is True


async def test_a_restart_waits_for_a_turn_this_device_is_driving(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    assert await harness.service._restart_safe() is False
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await settle(lambda: bool(harness.events("turn_completed")))
    assert await harness.service._restart_safe() is True


async def test_a_scan_that_could_not_finish_is_never_permission_to_restart(
    harness: Harness,
) -> None:
    """The terminal scan fails closed: an incomplete answer is not "nobody is there"."""
    harness.terminals.cwds = set()
    harness.terminals.complete = False
    assert await harness.service._restart_safe() is False


# ----------------------------------------------- a thread that speaks is alive


CHILD = "01a08c41-77e2-7d10-9f34-2b5c8ae61d02"
INTERRUPT_REQUEST = "5e0c2a9d-71b4-4f36-a8e2-3d9b6c1f0a47"


def child_row(parent: str = THREAD, thread_id: str = CHILD, **extra: Any) -> dict[str, Any]:
    """A thread Codex opened for a subagent, as the index describes one.

    The parent is named twice: on the thread itself and inside the `source`
    object every subagent carries instead of a plain string.
    """
    row = thread_row(
        thread_id,
        preview="Review the diff",
        status={"type": "active"},
        parentThreadId=parent,
        source={"subAgent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}},
    )
    row.update(extra)
    return row


async def archived_here(harness: Harness) -> None:
    """A session started here, archived, then resumed in a terminal."""
    harness.registry.upsert_session(
        Session(
            session_id=THREAD,
            device_id="dev-1",
            agent="codex",
            cwd="/repo",
            state="idle",
            origin="remote",
            control="none",
            archived=True,
        )
    )
    harness.hub.load()
    assert harness.hub.entry(THREAD).session.state == "stopped"
    await started(harness, loaded=[])
    assert harness.hub.entry(THREAD).session.archived is True


async def test_a_turn_starting_takes_a_resumed_thread_out_of_the_archive(
    harness: Harness,
) -> None:
    """`codex resume <id>` on an archived thread, with no `thread/started` for it."""
    await archived_here(harness)
    await harness.daemon.notify(
        "turn/started", {"threadId": THREAD, "turn": {"id": "turn-5", "status": "inProgress"}}
    )
    await settle(
        lambda: (
            harness.hub.entry(THREAD).session.archived is False
            and harness.hub.entry(THREAD).session.state == "running"
        )
    )
    session = harness.hub.entry(THREAD).session
    assert session.state == "running"
    # Whoever spoke is not this device, which has no runner on the thread.
    assert session.control == "shared"

    await harness.daemon.notify(
        "item/completed",
        {"threadId": THREAD, "item": {"id": "a1", "type": "agentMessage", "text": "on it"}},
    )
    await settle(lambda: bool(harness.events("assistant_text")))


async def test_a_status_turning_active_takes_a_resumed_thread_out_of_the_archive(
    harness: Harness,
) -> None:
    await archived_here(harness)
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": THREAD, "status": {"type": "active"}}
    )
    await settle(lambda: harness.hub.entry(THREAD).session.archived is False)
    assert harness.hub.entry(THREAD).session.state == "running"
    assert harness.hub.entry(THREAD).session.control == "shared"


async def test_a_thread_that_is_only_loaded_stays_archived(harness: Harness) -> None:
    """Being in the daemon's loaded list is not speaking: the daemon never unloads."""
    await archived_here(harness)
    harness.daemon.replies["thread/loaded/list"] = {"data": [THREAD]}
    await harness.service.refresh()
    assert harness.hub.entry(THREAD).session.archived is True
    assert harness.hub.entry(THREAD).session.state == "stopped"


# ----------------------------------------------------- working means all of it


def test_a_subagents_thread_names_the_thread_that_spawned_it() -> None:
    assert threads.parent_of(child_row()) == THREAD
    # Either spelling alone is enough, and the `source` object is searched.
    assert threads.parent_of({"id": "x", "parentThreadId": THREAD}) == THREAD
    spawned = {"id": "x", "source": {"subagent": {"thread_spawn": {"parent_thread_id": THREAD}}}}
    assert threads.parent_of(spawned) == THREAD
    assert threads.parent_of(thread_row()) is None
    assert threads.parent_of({"id": "x", "source": "cli"}) is None


def test_the_items_that_name_a_subagent_say_whether_it_is_working() -> None:
    spawn = {
        "type": "collabAgentToolCall",
        "tool": "spawn_agent",
        "senderThreadId": THREAD,
        "receiverThreadIds": [CHILD],
        "agentsStates": {CHILD: "in_progress"},
    }
    assert children.child_states(spawn, completed=False) == {CHILD: True}
    done = dict(spawn, agentsStates={CHILD: "completed"})
    assert children.child_states(done, completed=True) == {CHILD: False}
    # A `close_agent` call ends every agent it addresses, states or no states.
    closing = {"type": "collabAgentToolCall", "tool": "close_agent", "receiverThreadIds": [CHILD]}
    assert children.child_states(closing, completed=True) == {CHILD: False}
    # An activity row is work under way until the row itself completes.
    activity = {"type": "subAgentActivity", "agentThreadId": CHILD, "agentPath": "reviewer"}
    assert children.child_states(activity, completed=False) == {CHILD: True}
    assert children.child_states(activity, completed=True) == {CHILD: False}
    assert children.child_states({"type": "agentMessage", "text": "hi"}, completed=True) == {}


def test_a_subagent_that_has_gone_silent_stops_holding_its_parent() -> None:
    watch = children.ChildWatch(stale_after=0.0)
    watch.note(CHILD, True)
    assert watch.working is False
    kept = children.ChildWatch(stale_after=60.0)
    kept.note(CHILD, True)
    assert kept.working is True and kept.count == 1
    assert kept.note(CHILD, False) is True
    assert kept.working is False


async def working_parent(harness: Harness) -> None:
    """A turn this device started, with one subagent thread running under it."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": SEND_REQUEST, "session_id": THREAD, "text": "review it"})
    await settle(lambda: harness.hub.entry(THREAD).session.state == "running")
    await harness.daemon.notify("thread/started", {"thread": child_row()})
    await settle(lambda: harness.service.child_parent(CHILD) == THREAD)


async def test_a_subagents_thread_never_becomes_a_session(harness: Harness) -> None:
    await working_parent(harness)
    assert CHILD not in harness.hub.entries
    assert harness.service.knows(CHILD) is False


async def test_a_turn_does_not_end_while_a_subagent_is_still_working(
    harness: Harness,
) -> None:
    await working_parent(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.15)
    assert harness.events("turn_completed") == []
    assert harness.hub.entry(THREAD).session.state == "running"

    # What the parent does next belongs to the same turn, not to a second one.
    await harness.daemon.notify(
        "turn/started", {"threadId": THREAD, "turn": {"id": "turn-2", "status": "inProgress"}}
    )
    await asyncio.sleep(0.1)
    assert len(harness.events("turn_started")) == 1

    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-2", "status": "completed"}}
    )
    await harness.daemon.notify(
        "turn/completed", {"threadId": CHILD, "turn": {"id": "c-1", "status": "completed"}}
    )
    await settle(lambda: bool(harness.events("turn_completed")))
    assert len(harness.events("turn_completed")) == 1
    assert harness.events("turn_completed")[-1]["stop_reason"] == "completed"
    assert harness.hub.entry(THREAD).session.state == "idle"


async def test_a_message_sent_while_subagents_work_waits_for_them(harness: Harness) -> None:
    await working_parent(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.1)
    result = await harness.hub.send(
        {"id": STEER_REQUEST, "session_id": THREAD, "text": "and this too", "mode": "queue"}
    )
    assert result["accepted"] == "queued"

    await harness.daemon.notify(
        "thread/closed", {"threadId": CHILD, "turn": {"id": "c-1", "status": "completed"}}
    )
    await settle(lambda: bool(harness.events("turn_completed")))
    assert harness.service.child_parent(CHILD) is None


async def test_a_subagent_the_parents_own_items_name_holds_the_turn_open(
    harness: Harness,
) -> None:
    """The daemon may send us nothing of the child; the parent's timeline names it."""
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": SEND_REQUEST, "session_id": THREAD, "text": "review it"})
    await settle(lambda: harness.hub.entry(THREAD).session.state == "running")
    spawn = {
        "id": "tool-1",
        "type": "collabAgentToolCall",
        "tool": "spawn_agent",
        "senderThreadId": THREAD,
        "receiverThreadIds": [CHILD],
        "agents_states": {CHILD: "in_progress"},
        "status": "inProgress",
    }
    await harness.daemon.notify("item/started", {"threadId": THREAD, "item": spawn})
    await settle(lambda: bool(harness.events("tool_call")))

    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.15)
    assert harness.events("turn_completed") == []
    assert harness.hub.entry(THREAD).session.state == "running"

    finished = dict(spawn, agents_states={CHILD: "completed"}, status="completed")
    await harness.daemon.notify("item/completed", {"threadId": THREAD, "item": finished})
    await settle(lambda: bool(harness.events("turn_completed")))
    assert len(harness.events("turn_completed")) == 1


async def test_a_subagent_of_a_thread_that_is_not_ours_is_ignored(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    stranger = child_row(parent="t-somebody-else", thread_id="t-their-child")
    await harness.daemon.notify("thread/started", {"thread": stranger})
    await asyncio.sleep(0.15)
    assert "t-their-child" not in harness.hub.entries
    assert harness.service.child_parent("t-their-child") is None


async def test_stopping_a_turn_its_subagents_hold_open_ends_it(harness: Harness) -> None:
    await working_parent(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.1)
    assert harness.events("turn_completed") == []

    runner = harness.hub.entry(THREAD).runner
    assert isinstance(runner, CodexDaemonSession)
    assert await runner.interrupt() is True
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    assert harness.hub.entry(THREAD).session.state == "idle"
    # There is no turn on this thread to stop: the parent's own turn is over.
    assert harness.daemon.sent("turn/interrupt") == []


async def test_interrupt_and_send_on_a_turn_its_subagents_hold_open_sends(
    harness: Harness,
) -> None:
    """Stopping the held turn ends it inline, inside the send that holds the lock.

    The turn's end only schedules the drain, so the send goes on to deliver
    its own message, and the one already held waits for that turn (A43).
    """
    await working_parent(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.1)
    queued = await harness.hub.send(
        {"id": STEER_REQUEST, "session_id": THREAD, "text": "and this too", "mode": "queue"}
    )
    assert queued["accepted"] == "queued"

    result = await asyncio.wait_for(
        harness.hub.send(
            {
                "id": INTERRUPT_REQUEST,
                "session_id": THREAD,
                "text": "this instead",
                "mode": "interrupt",
            }
        ),
        timeout=2,
    )

    assert result == {"accepted": "sent"}
    assert harness.events("turn_completed")[-1]["stop_reason"] == "interrupted"
    assert harness.daemon.sent("turn/start")[-1]["input"][0]["text"] == "this instead"
    entry = harness.hub.entry(THREAD)
    await drained(entry)
    assert [item["id"] for item in entry.queue] == [STEER_REQUEST]


async def test_a_subagent_that_says_nothing_for_long_enough_lets_the_turn_end(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing in Codex marks a killed subagent, so silence has to have a bound."""
    monkeypatch.setattr(children, "CHILD_STALE_S", 0.3)
    await working_parent(harness)
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await asyncio.sleep(0.4)
    assert harness.events("turn_completed") == []
    await harness.service.refresh()
    await settle(lambda: bool(harness.events("turn_completed")))
    assert harness.hub.entry(THREAD).session.state == "idle"


# ----------------------------------- A39: closing a session the device drives


async def device_driven(harness: Harness) -> None:
    """A thread this device drives: loaded, subscribed, no terminal in it."""
    await started(harness, loaded=[THREAD])
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    assert harness.hub.entry(THREAD).session.control == "none"


async def test_closing_a_thread_interrupts_the_turn_then_archives_it(harness: Harness) -> None:
    await device_driven(harness)
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    assert harness.hub.entry(THREAD).session.control == "remote"

    closing = asyncio.create_task(harness.hub.archive({"session_id": THREAD, "archived": True}))
    await harness.daemon.wait_for_call("turn/interrupt")
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    result = await closing

    asked = [
        method
        for method, _ in harness.daemon.calls
        if method in {"turn/interrupt", "thread/archive", "thread/unsubscribe"}
    ]
    assert asked == ["turn/interrupt", "thread/archive", "thread/unsubscribe"]
    assert harness.daemon.sent("thread/archive")[-1] == {"threadId": THREAD}
    session = result["session"]
    assert (session["archived"], session["control"], session["state"]) == (True, "none", "stopped")
    assert harness.hub.entry(THREAD).runner is None


async def test_a_thread_s_last_word_during_a_close_does_not_revive_it(harness: Harness) -> None:
    await device_driven(harness)
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    closing = asyncio.create_task(harness.hub.archive({"session_id": THREAD, "archived": True}))
    await harness.daemon.wait_for_call("turn/interrupt")
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-1", "status": "completed"}}
    )
    await closing

    # Everything the thread says on its way out: a status, and the archive
    # itself. None of it is the session coming back to life (A15 does not fire).
    await harness.daemon.notify(
        "thread/status/changed", {"threadId": THREAD, "status": {"type": "active"}}
    )
    await harness.daemon.notify("thread/archived", {"threadId": THREAD})
    await asyncio.sleep(0.15)

    session = harness.hub.entry(THREAD).session
    assert (session.archived, session.state, session.control) == (True, "stopped", "none")
    assert harness.hub.entry(THREAD).runner is None


async def test_a_closed_thread_leaves_the_index_and_keeps_its_row(harness: Harness) -> None:
    """Codex hides an archived thread, and a hidden thread is not a deleted one."""
    await device_driven(harness)
    await harness.hub.archive({"session_id": THREAD, "archived": True})
    await harness.daemon.notify("thread/archived", {"threadId": THREAD})
    await settle(lambda: harness.service.knows(THREAD) is False)

    harness.daemon.replies["thread/list"] = {"data": [thread_row("t-other")]}
    harness.daemon.replies["thread/loaded/list"] = {"data": ["t-other"]}
    await harness.service.refresh()

    assert THREAD in harness.hub.entries
    assert harness.hub.entry(THREAD).session.archived is True
    assert {"type": "session.removed", "session_id": THREAD} not in harness.frames


async def test_writing_to_a_closed_session_unarchives_the_thread(harness: Harness) -> None:
    await device_driven(harness)
    await harness.hub.archive({"session_id": THREAD, "archived": True})
    harness.daemon.errors["thread/resume"] = (
        f"session {THREAD} is archived. Run `codex unarchive {THREAD}` to unarchive it first."
    )

    def responder(method: str, params: dict[str, Any]) -> Any:
        if method == "thread/unarchive":
            harness.daemon.errors.pop("thread/resume", None)
            return {}
        return None

    harness.daemon.responder = responder

    await harness.hub.send({"id": "req-9", "session_id": THREAD, "text": "carry on"})

    assert harness.daemon.sent("thread/unarchive")[-1] == {"threadId": THREAD}
    session = harness.hub.entry(THREAD).session
    assert session.archived is False
    assert session.control == "remote"


async def test_a_resume_refused_for_any_other_reason_is_not_unarchived(harness: Harness) -> None:
    """A thread too young to resume refuses every time; asking to unarchive it helps nobody."""
    await device_driven(harness)
    await harness.hub.archive({"session_id": THREAD, "archived": True})
    harness.daemon.errors["thread/resume"] = "thread has no rollout yet"

    await harness.hub.send({"id": "req-9", "session_id": THREAD, "text": "carry on"})

    assert harness.daemon.sent("thread/unarchive") == []


async def test_a_session_reopened_after_a_close_is_the_device_s_again(harness: Harness) -> None:
    """The index forgot the thread; the live subscription is what says it is back."""
    await device_driven(harness)
    await harness.hub.archive({"session_id": THREAD, "archived": True})
    await harness.daemon.notify("thread/archived", {"threadId": THREAD})
    await settle(lambda: harness.service.knows(THREAD) is False)

    await harness.hub.send({"id": "req-9", "session_id": THREAD, "text": "carry on"})

    session = harness.hub.entry(THREAD).session
    assert (session.archived, session.control) == (False, "remote")
