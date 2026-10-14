"""The session behind a terminal changes, and the session it left goes away.

Claude Code hands an MCP server the environment it was spawned with, so the
channel bridge names the session the CLI started on for as long as it runs. Its
`SessionStart` hook fires again on every `/resume`, `/clear` and `/compact`, and
is the only account of those the device gets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import holders as claude_holders
from rc_client.agents.claude import transcripts
from rc_client.agents.codex import rollouts
from rc_client.models import AgentInfo, Choice, Session
from rc_client.registry import Registry
from rc_client.sessions import hub as hub_module
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.attach import Attachment, SessionStart
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.mirror import MirrorService

TERMINAL_PID = 100


class FakeAttachment(Attachment):
    """An attachment that records what it would have sent to the bridge."""

    def __init__(self, session_id: str, pid: int = TERMINAL_PID, cwd: str = "/repo") -> None:
        super().__init__(session_id=session_id, cwd=cwd, pid=pid, claude_version="2.1.268")
        self.injected: list[tuple[str, str]] = []
        self.reachable = True

    async def inject(self, message_id: str, text: str) -> bool:
        self.injected.append((message_id, text))
        return self.reachable

    async def relay_permission(self, request_id: str, behavior: str) -> bool:
        return True

    def detach(self) -> None:
        self.reachable = False


def agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            capabilities=["takeover", "interrupt", "queue", "history"],
            attach="channel",
            attach_ready=True,
        )
    ]


class Harness:
    """A hub, the frames it published, and the terminals attached to it."""

    def __init__(self, tmp_path: Path) -> None:
        self.frames: list[dict[str, Any]] = []
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", agents)

    async def attach(self, session_id: str, pid: int = TERMINAL_PID) -> FakeAttachment:
        attachment = FakeAttachment(session_id, pid=pid)
        await self.hub.attach_registered(attachment)
        return attachment

    async def hook(
        self,
        session_id: str,
        *,
        source: str = "resume",
        pid: int = TERMINAL_PID,
        cwd: str = "/repo",
        transcript_path: str = "",
    ) -> None:
        await self.hub.attach_session_started(
            SessionStart(
                session_id=session_id,
                cwd=cwd,
                pid=pid,
                source=source,
                transcript_path=transcript_path,
            )
        )

    def seed(self, session_id: str, **fields: Any) -> SessionEntry:
        """A session the device already knows, as `hub.load` would have left it."""
        session = Session(
            session_id=session_id,
            device_id="dev-1",
            agent=str(fields.pop("agent", "claude")),
            cwd="/repo",
            state="idle",
            origin=fields.pop("origin", "terminal"),
            control="none",
            **fields,
        )
        entry = self.hub.register_mirrored(session)
        return entry

    def removed(self) -> list[str]:
        return [
            str(frame["session_id"])
            for frame in self.frames
            if frame.get("type") == "session.removed"
        ]

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # No session has a transcript unless a test says so, and a closing bridge
    # never asks the process table about the developer's own terminals.
    monkeypatch.setattr(transcripts, "find_transcript", lambda session_id: None)
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)
    built = Harness(tmp_path)
    yield built
    built.close()


def no_holder(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI whose bridge just closed is gone from the process table."""

    async def gone(entry: SessionEntry) -> str:
        entry.holder_pid = None
        entry.holder_identity = None
        return "none"

    monkeypatch.setattr(harness.hub, "_holder_control", gone)


async def said(entry: SessionEntry, text: str) -> None:
    """One stored event, the way a terminal turn leaves one behind."""
    await entry.channel.emit("user_message", block_id="b1", text=text, source="terminal")


# ------------------------------------------------------- the hook and the bridge


async def test_a_bridge_lands_on_the_session_its_hook_already_named(harness: Harness) -> None:
    """The hook usually wins the race: the id in the bridge's environment is stale."""
    await harness.hook("resumed-1", source="resume", transcript_path="/t/resumed-1.jsonl")
    attachment = await harness.attach("startup-1")

    assert "startup-1" not in harness.hub.entries
    assert attachment.session_id == "resumed-1"
    entry = harness.hub.entry("resumed-1")
    assert entry.session.control == "shared"
    assert entry.transcript == "/t/resumed-1.jsonl"
    assert harness.removed() == []


async def test_a_startup_hook_for_the_session_we_are_on_changes_nothing(harness: Harness) -> None:
    attachment = await harness.attach("startup-1")
    await harness.hook("startup-1", source="startup")

    assert list(harness.hub.entries) == ["startup-1"]
    assert attachment.session_id == "startup-1"
    assert harness.hub.entry("startup-1").session.control == "shared"
    assert harness.removed() == []


async def test_a_resume_moves_the_attachment_and_the_empty_session_goes_away(
    harness: Harness,
) -> None:
    attachment = await harness.attach("startup-1")
    await harness.hook("resumed-1", source="resume", transcript_path="/t/resumed-1.jsonl")

    assert attachment.session_id == "resumed-1"
    assert "startup-1" not in harness.hub.entries
    assert harness.removed() == ["startup-1"]
    moved = harness.hub.entry("resumed-1")
    assert moved.session.control == "shared"
    assert moved.session.origin == "terminal"
    assert moved.holder_pid == TERMINAL_PID
    assert moved.shared is not None and moved.shared.attachment is attachment


async def test_a_clear_leaves_a_used_session_behind_under_nobodys_control(
    harness: Harness,
) -> None:
    attachment = await harness.attach("startup-1")
    entry = harness.hub.entry("startup-1")
    await said(entry, "what did we decide")

    await harness.hook("cleared-1", source="clear")

    assert attachment.session_id == "cleared-1"
    assert harness.removed() == []
    assert entry.session.control == "none"
    assert entry.session.state == "idle"
    assert entry.shared is None
    assert (entry.holder_pid, entry.holder_identity) == (None, None)
    assert harness.hub.entry("cleared-1").session.control == "shared"


async def test_a_session_moved_back_onto_keeps_its_title_and_its_history(
    harness: Harness,
) -> None:
    older = harness.seed("older-1", title="Ledger migration")
    older.channel.start()
    await said(older, "where were we")

    await harness.attach("startup-1")
    await harness.hook("older-1", source="resume")

    assert harness.hub.entry("older-1") is older
    assert older.session.title == "Ledger migration"
    assert older.session.control == "shared"
    history, _ = harness.registry.history("older-1")
    assert [event["text"] for event in history] == ["where were we"]
    assert harness.removed() == ["startup-1"]


async def test_a_session_this_device_drives_is_never_moved_onto(harness: Harness) -> None:
    driven = harness.seed("driven-1", origin="remote")
    driven.runner = object()  # type: ignore[assignment]
    attachment = await harness.attach("startup-1")

    await harness.hook("driven-1", source="resume")

    assert attachment.session_id == "startup-1"
    assert harness.hub.entry("startup-1").session.control == "shared"
    assert harness.removed() == []


# ------------------------------------------------------------- a bridge closing


async def test_closing_a_bridge_that_said_nothing_removes_the_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = await harness.attach("startup-1")
    # Why the ghost test reads the stored events rather than `last_seq`: an
    # attachment publishes `meta` and `status` events about itself, which take a
    # sequence number each and are never stored.
    assert harness.registry.last_seq("startup-1") > 0
    assert harness.registry.has_events("startup-1") is False
    no_holder(harness, monkeypatch)

    await harness.hub.attach_closed(attachment)

    assert "startup-1" not in harness.hub.entries
    assert harness.removed() == ["startup-1"]


async def test_closing_a_bridge_on_a_used_session_keeps_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = await harness.attach("startup-1")
    await said(harness.hub.entry("startup-1"), "run the tests")
    no_holder(harness, monkeypatch)

    await harness.hub.attach_closed(attachment)

    assert harness.hub.entry("startup-1").session.control == "none"
    assert harness.removed() == []


async def test_a_closed_bridge_is_forgotten_so_its_pid_can_be_reused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recycled pid must not drag the next terminal into the last one's session."""
    attachment = await harness.attach("startup-1")
    await harness.hook("resumed-1", source="resume")
    no_holder(harness, monkeypatch)
    await harness.hub.attach_closed(attachment)

    fresh = await harness.attach("startup-2")
    assert fresh.session_id == "startup-2"


# --------------------------------------------------------------------- the sweep


async def test_the_sweep_removes_ghosts_and_leaves_everything_else(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.seed("ghost-1")
    harness.seed("ghost-2")
    used = harness.seed("used-1")
    used.channel.start()
    await said(used, "still here")
    mirrored = harness.seed("mirrored-1")
    mirrored.transcript = "/t/mirrored-1.jsonl"
    harness.seed("on-disk-1")
    harness.seed("codex-1", agent="codex")
    harness.seed("remote-1", origin="remote")
    await harness.attach("attached-1")
    monkeypatch.setattr(
        transcripts,
        "find_transcript",
        lambda session_id: Path("/t/on-disk-1.jsonl") if session_id == "on-disk-1" else None,
    )

    await harness.hub.sweep_ghosts()

    assert sorted(harness.hub.entries) == [
        "attached-1",
        "codex-1",
        "mirrored-1",
        "on-disk-1",
        "remote-1",
        "used-1",
    ]
    assert sorted(harness.removed()) == ["ghost-1", "ghost-2"]


async def test_a_removal_the_link_was_down_for_is_repeated_on_the_next_one(
    harness: Harness,
) -> None:
    """The gateway keeps every session a device announced; a dropped removal lingers."""
    harness.seed("ghost-1")
    await harness.hub.sweep_ghosts()
    assert harness.removed() == ["ghost-1"]

    harness.hub.snapshot()  # the `hello` of a new link
    await harness.hub.sweep_ghosts()
    assert harness.removed() == ["ghost-1", "ghost-1"]

    await harness.hub.sweep_ghosts()
    assert harness.removed() == ["ghost-1", "ghost-1"], "one repeat per link, not one per scan"


async def test_a_repeat_waits_for_the_link_to_be_up(harness: Harness) -> None:
    """A repeat published into a gap would be dropped, and the record with it."""
    harness.seed("ghost-1")
    await harness.hub.sweep_ghosts()
    assert harness.removed() == ["ghost-1"]

    harness.hub.link_up = lambda: False
    harness.hub.snapshot()  # a `hello` is being built; the ack has not arrived
    await harness.hub.sweep_ghosts()
    assert harness.removed() == ["ghost-1"], "nothing goes out while the link is down"

    harness.hub.link_up = lambda: True
    await harness.hub.sweep_ghosts()
    assert harness.removed() == ["ghost-1", "ghost-1"]


async def test_the_scan_leaves_the_session_a_terminal_moved_away_from_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a `/clear`, the CLI belongs to the session it moved to and to no other."""
    monkeypatch.setattr(hub_module, "EXIT_SETTLE", 0.0)
    path = tmp_path / "startup-1.jsonl"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        transcripts,
        "find_transcript",
        lambda session_id: path if session_id == "startup-1" else None,
    )
    harness = Harness(tmp_path)
    try:
        await harness.attach("startup-1")
        await harness.hook("cleared-1", source="clear")

        mirror = MirrorService(harness.hub)
        info = transcripts.TranscriptInfo(
            session_id="startup-1", path=str(path), cwd="/repo", size=0, mtime=path.stat().st_mtime
        )
        monkeypatch.setattr(transcripts, "discover", lambda *args: [info])
        monkeypatch.setattr(rollouts, "discover", lambda *args: [])

        async def scan() -> claude_holders.HolderScan:
            return claude_holders.HolderScan(
                holders=[
                    claude_holders.Holder(
                        pid=TERMINAL_PID,
                        identity=(TERMINAL_PID, "a"),
                        session_id=None,
                        cwd="/repo",
                        attachable=True,
                    )
                ],
                complete=True,
            )

        monkeypatch.setattr(mirror_module, "scan_holders", scan)
        await mirror.scan_once()

        assert harness.hub.entry("cleared-1").session.control == "shared"
        left = harness.hub.entry("startup-1")
        assert left.session.control == "none"
        assert left.session.state == "idle"
    finally:
        await harness.hub.close()
        harness.close()
