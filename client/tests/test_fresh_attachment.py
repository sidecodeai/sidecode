"""A40: a session attached before it has a transcript is read from its first row."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts
from rc_client.agents.claude.holders import HolderScan
from rc_client.agents.codex import rollouts
from rc_client.agents.grok import sessions as grok_sessions
from rc_client.models import Session
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions import typist
from rc_client.sessions.mirror import MirrorService
from rc_client.sessions.ptys import TerminalState
from tests.test_typist import FakeTerminal, Harness

SESSION = "sess-1"


def command_rows(answer: str) -> list[dict[str, Any]]:
    """What Claude Code writes for a typed `/effort`: the command, then its answer."""
    said = [
        "<command-name>/effort</command-name>\n<command-message>effort</command-message>\n"
        "<command-args></command-args>",
        f"<local-command-stdout>{answer}</local-command-stdout>",
    ]
    return [
        {
            "type": "user",
            "uuid": f"row-{index}",
            "sessionId": SESSION,
            "cwd": "/repo",
            "message": {"role": "user", "content": text},
        }
        for index, text in enumerate(said)
    ]


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class FreshTerminal(FakeTerminal):
    """A CLI that has written nothing until the device's `/effort` takes."""

    def __init__(self, transcript: Path) -> None:
        super().__init__(new_slider=True, level=1)
        self.transcript = transcript

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        state = await super().keys(data, clear)
        if self.applied_effort is not None and not self.transcript.exists():
            write_rows(
                self.transcript,
                command_rows(f"Set effort level to {self.applied_effort} (this session only)"),
            )
        return state


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Claude's projects in a scratch folder, and nothing else on this machine to find."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", root)
    monkeypatch.setattr(rollouts, "discover", lambda *args: [])
    monkeypatch.setattr(grok_sessions, "discover", lambda *args: [])

    async def no_terminal() -> HolderScan:
        return HolderScan(holders=[], complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", no_terminal)
    return root


async def test_the_first_effort_on_a_fresh_session_is_confirmed_from_its_new_transcript(
    tmp_path: Path, projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file appears with the command's own rows; nothing waits for a scan or a pass."""
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.05)
    monkeypatch.setattr(typist, "CONFIRM_TIMEOUT", 2.0)
    # Neither a scan nor an ordinary tail pass comes round during the test.
    monkeypatch.setattr(mirror_module, "SCAN_INTERVAL", 3600.0)
    monkeypatch.setattr(mirror_module, "TAIL_INTERVAL", 3600.0)
    harness = Harness(tmp_path)
    mirror = MirrorService(harness.hub)
    mirror.start()
    entry = await harness.attach(FreshTerminal(projects / "-repo" / f"{SESSION}.jsonl"))

    reply = await asyncio.wait_for(
        harness.hub.set_options({"session_id": SESSION, "effort": "max"}), timeout=5
    )

    assert reply["session"]["effort"] == "max"
    assert entry.session.effort == "max"
    assert entry.transcript == str(projects / "-repo" / f"{SESSION}.jsonl")
    assert harness.events("user_message") == [], "the device's own keystrokes draw no bubble"
    await mirror.stop()
    await harness.hub.close()
    harness.close()


async def test_a_tail_pass_reads_an_attached_session_no_scan_has_found(
    tmp_path: Path, projects: Path
) -> None:
    harness = Harness(tmp_path)
    mirror = MirrorService(harness.hub)
    entry = await harness.attach()
    await mirror.tail_once()
    assert entry.transcript is None, "nothing to read before the CLI writes a row"

    write_rows(
        projects / "-repo" / f"{SESSION}.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": SESSION,
                "cwd": "/repo",
                "message": {"role": "user", "content": "what is in this folder?"},
            }
        ],
    )
    await mirror.tail_once()

    assert [event["text"] for event in harness.events("user_message")] == [
        "what is in this folder?"
    ]
    assert entry.shared is not None and entry.shared.running is True
    await harness.hub.close()
    harness.close()


async def test_a_session_nothing_is_attached_to_waits_for_the_scan(
    tmp_path: Path, projects: Path
) -> None:
    """Only an attached session is looked for by id; the scan finds the rest."""
    harness = Harness(tmp_path)
    harness.hub.register_mirrored(
        Session(
            session_id="idle-1",
            device_id="dev-1",
            agent="claude",
            cwd="/repo",
            origin="terminal",
            control="none",
        )
    )
    write_rows(
        projects / "-repo" / "idle-1.jsonl",
        [{"type": "user", "uuid": "u1", "sessionId": "idle-1", "cwd": "/repo", "message": {}}],
    )
    mirror = MirrorService(harness.hub)
    await mirror.tail_once()
    assert harness.hub.entry("idle-1").transcript is None
    await harness.hub.close()
    harness.close()


async def test_attaching_wakes_the_reader(tmp_path: Path) -> None:
    """A resumed session's transcript is already there, and is read at once."""
    harness = Harness(tmp_path)
    woken: list[bool] = []

    class Reader:
        def read_soon(self) -> None:
            woken.append(True)

        def settle_soon(self, session_id: str, delay: float) -> None:
            pass

    harness.hub.shared.reader = Reader()
    await harness.attach()
    assert woken == [True]
    await harness.hub.close()
    harness.close()
