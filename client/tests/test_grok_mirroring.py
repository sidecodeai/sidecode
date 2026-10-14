"""Mirroring a Grok session a person is running in a terminal."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts as claude_transcripts
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok import sessions as grok_sessions
from rc_client.config import MirrorConfig
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService

SESSION = "01a09b4f-1b25-7e92-b96b-356d292ff2d0"
CWD = "/Users/me/dev/gateway"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"


def fixture_rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (FIXTURES / "terminal-updates.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_session(
    home: Path,
    session_id: str = SESSION,
    cwd: str = CWD,
    rows: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> Path:
    """One session directory, laid out the way Grok lays one out."""
    directory = home / "sessions" / urllib.parse.quote(cwd, safe="") / session_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps(
            summary
            if summary is not None
            else {
                "info": {"id": session_id, "cwd": cwd},
                "generated_title": "Uninstalling the Codex CLI",
                "current_model_id": "grok-4.6",
                "reasoning_effort": "xhigh",
            }
        ),
        encoding="utf-8",
    )
    path = directory / "updates.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows if rows is not None else fixture_rows():
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


class Recorder:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "grok"
    monkeypatch.setattr(grok_runtime, "home", lambda: directory)
    # A scan looks at all three agents; the other two belong to the developer.
    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: [])
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])
    return directory


def build(tmp_path: Path) -> tuple[MirrorService, SessionHub, Recorder]:
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    info = AgentInfo(agent="grok", available=True)
    hub = SessionHub(registry, recorder, "d1", lambda: [info])
    mirror = MirrorService(hub, MirrorConfig())
    return mirror, hub, recorder


# ---------------------------------------------------------------- discovery


def test_discovery_reads_the_summary_grok_writes(home: Path) -> None:
    write_session(home)
    found = grok_sessions.discover()
    assert len(found) == 1
    info = found[0]
    assert info.session_id == SESSION
    assert info.cwd == CWD
    assert info.title == "Uninstalling the Codex CLI"
    assert info.settings == {"model": "grok-4.6", "effort": "xhigh"}


def test_a_session_with_no_summary_still_names_its_directory(home: Path) -> None:
    path = write_session(home, summary={})
    assert path.exists()
    info = grok_sessions.discover()[0]
    # The encoded parent directory is the working directory.
    assert info.cwd == CWD
    assert info.title == ""


def test_an_empty_log_is_not_a_session(home: Path) -> None:
    write_session(home, rows=[])
    assert grok_sessions.discover() == []


def test_the_live_registry_is_read_when_it_is_there(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    assert grok_sessions.active_ids() == (set(), False)
    (home / "active_sessions.json").write_text(json.dumps([]), encoding="utf-8")
    assert grok_sessions.active_ids() == (set(), True)
    (home / "active_sessions.json").write_text(
        json.dumps([{"session_id": SESSION}, "other"]), encoding="utf-8"
    )
    ids, known = grok_sessions.active_ids()
    assert known is True
    assert ids == {SESSION, "other"}


# ------------------------------------------------------------------ tailing


async def test_a_terminal_session_is_adopted_with_its_title_and_settings(
    home: Path, tmp_path: Path
) -> None:
    write_session(home)
    mirror, hub, recorder = build(tmp_path)
    await mirror.scan_once()
    entry = hub.entries[SESSION]
    assert entry.session.agent == "grok"
    assert entry.session.origin == "terminal"
    assert entry.session.cwd == CWD
    assert entry.session.title == "Uninstalling the Codex CLI"
    assert entry.session.model == "grok-4.6"
    assert entry.session.effort == "xhigh"

    await mirror.tail_once()
    assert recorder.events("user_message")[0]["source"] == "terminal"
    assert recorder.events("assistant_text")[-1]["text"] == "OK"
    assert recorder.events("turn_completed")[0]["stop_reason"] == "completed"
    assert entry.session.state in {"idle", "readonly"}


async def test_a_turn_in_progress_reads_as_running_and_then_stops(
    home: Path, tmp_path: Path
) -> None:
    rows = fixture_rows()
    started = [row for row in rows if "user_message_chunk" in json.dumps(row)]
    write_session(home, rows=started)
    mirror, hub, _ = build(tmp_path)
    await mirror.scan_once()
    entry = hub.entries[SESSION]
    entry.session.control = "terminal"
    await mirror.tail_once()
    assert mirror._grok[SESSION].tailer.running is True

    with (home / "sessions" / urllib.parse.quote(CWD, safe="") / SESSION / "updates.jsonl").open(
        "a", encoding="utf-8"
    ) as handle:
        for row in rows:
            if "turn_completed" in json.dumps(row):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    await mirror.tail_once()
    assert mirror._grok[SESSION].tailer.running is False
    assert entry.session.state == "readonly"


async def test_a_mirror_that_attaches_mid_turn_still_reads_as_running(
    home: Path, tmp_path: Path
) -> None:
    """Every row of a turn names its prompt, even when the message row is long gone."""
    thoughts = [row for row in fixture_rows() if "agent_thought_chunk" in json.dumps(row)]
    assert thoughts
    write_session(home, rows=thoughts)
    mirror, hub, _ = build(tmp_path)
    await mirror.scan_once()
    hub.entries[SESSION].session.control = "terminal"
    await mirror.tail_once()
    assert mirror._grok[SESSION].tailer.running is True


async def test_a_tail_resumes_from_the_event_id_it_stopped_at(home: Path, tmp_path: Path) -> None:
    write_session(home)
    mirror, hub, recorder = build(tmp_path)
    await mirror.scan_once()
    await mirror.tail_once()
    cursor = mirror._grok[SESSION].tailer.cursor
    assert cursor > 0
    assert hub.registry.get_kv(f"grok-cursor:{SESSION}") == str(cursor)

    # A second mirror over the same directory starts from the stored cursor and
    # re-reads the file from the beginning: nothing is published twice.
    second, second_hub, second_recorder = build(tmp_path)
    second_hub.registry.set_kv(f"grok-cursor:{SESSION}", str(cursor))
    await second.scan_once()
    second._grok[SESSION].tailer.offset = 0
    await second.tail_once()
    assert second_recorder.events("assistant_text") == []
    assert len(recorder.events("assistant_text")) > 0


async def test_a_session_this_device_drives_is_never_ingested_from_disk(
    home: Path, tmp_path: Path
) -> None:
    write_session(home)
    mirror, hub, recorder = build(tmp_path)
    hub.register_mirrored(
        Session(session_id=SESSION, device_id="d1", agent="grok", cwd=CWD, origin="remote")
    )
    await mirror.scan_once()
    await mirror.tail_once()
    assert SESSION not in mirror._grok
    assert recorder.events("assistant_text") == []


def test_an_event_id_yields_its_counter() -> None:
    assert grok_sessions.event_index({"params": {"_meta": {"eventId": f"{SESSION}-42"}}}) == 42
    assert grok_sessions.event_index({"params": {"_meta": {"eventId": "nonsense"}}}) == 0
    assert grok_sessions.event_index({}) == 0
