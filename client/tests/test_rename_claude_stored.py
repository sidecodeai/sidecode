"""A55: renaming a Claude session no process holds, or one open where the device cannot reach."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import renaming, runtime
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.sessions import titles
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.rename_helpers import summaries, title_events
from tests.test_hub import build_hub

SESSION = "5f8f6a02-1c9d-4a1e-9d4a-2f1e4c7b90aa"
LEDGER = "Ledger migration"


def project_transcript(body: str | None = None) -> Path:
    project = runtime.PROJECTS_DIR / "-repo"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{SESSION}.jsonl"
    path.write_text(
        body if body is not None else json.dumps({"type": "user", "uuid": "u1"}) + "\n",
        encoding="utf-8",
    )
    return path


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def held_by(hub: SessionHub, control: str = "none") -> SessionEntry:
    """A session the device knows from its transcript, with `control` as the scan left it."""
    return hub.register_mirrored(
        Session(
            session_id=SESSION,
            device_id="dev-1",
            agent="claude",
            cwd="/repo",
            origin="terminal",
            control=control,  # type: ignore[arg-type]
            updated_at=1_000,
        )
    )


def ask(title: str = LEDGER) -> dict[str, Any]:
    return {"session_id": SESSION, "title": title}


async def test_a_session_nothing_holds_gets_both_rows_of_a_rename(tmp_path: Path) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    path = project_transcript()
    entry = held_by(hub)

    result = await hub.set_options(ask())

    assert rows(path)[1:] == [
        {"type": "custom-title", "customTitle": LEDGER, "sessionId": SESSION},
        {"type": "agent-name", "agentName": LEDGER, "sessionId": SESSION},
    ]
    assert result["session"]["title"] == LEDGER
    assert titles.pinned(registry_db, SESSION)
    assert title_events(frames) == [LEDGER]
    assert result["session"]["updated_at"] == 1_000
    assert {summary["updated_at"] for summary in summaries(frames)} == {1_000}
    assert not entry.lock.locked()
    registry_db.close()


async def test_asking_again_for_the_name_it_holds_writes_nothing_more(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    held_by(hub)

    await hub.set_options(ask())
    await hub.set_options(ask(f"  {LEDGER}  "))

    assert len(rows(path)) == 3
    registry_db.close()


async def test_a_transcript_cut_short_is_sealed_by_the_rename(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript('{"type":"user","uuid":"u1"}\n{"type":"assistant","uuid":"tor')
    held_by(hub)

    await hub.set_options(ask())

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == '{"type":"assistant","uuid":"tor'
    assert json.loads(lines[2])["type"] == "custom-title"
    registry_db.close()


async def test_a_session_with_no_transcript_cannot_be_named_and_none_is_made(
    tmp_path: Path,
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = held_by(hub)

    with pytest.raises(RcError) as caught:
        await hub.set_options(ask())

    assert (caught.value.code, caught.value.message) == ("unsupported", renaming.NO_TRANSCRIPT)
    assert entry.session.title == ""
    assert not titles.pinned(registry_db, SESSION)
    assert title_events(frames) == []
    assert not runtime.PROJECTS_DIR.exists()
    registry_db.close()


async def test_an_empty_transcript_is_not_a_session_either(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript("")
    held_by(hub)

    with pytest.raises(RcError) as caught:
        await hub.set_options(ask())

    assert caught.value.code == "unsupported"
    assert path.read_bytes() == b""
    registry_db.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a file it has no permission to")
async def test_a_transcript_that_cannot_be_written_is_unsupported_without_its_path(
    tmp_path: Path,
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    path.chmod(0o444)
    entry = held_by(hub)

    with pytest.raises(RcError) as caught:
        await hub.set_options(ask())

    assert (caught.value.code, caught.value.message) == ("unsupported", renaming.UNWRITABLE)
    assert str(path) not in caught.value.message
    assert entry.session.title == ""
    registry_db.close()


@pytest.mark.parametrize("control", ["terminal", "shared"])
async def test_a_live_claude_the_device_cannot_reach_is_refused_and_untouched(
    tmp_path: Path, control: str
) -> None:
    """D4: written under a running CLI, a name would not be the CLI's own; the person
    is told to rename it where it runs."""
    hub, frames, registry_db = build_hub(tmp_path)
    path = project_transcript()
    before = path.read_bytes()
    entry = held_by(hub, control)

    with pytest.raises(RcError) as caught:
        await hub.set_options(ask())

    assert caught.value.code == "unsupported"
    assert "rename it there" in caught.value.message
    assert path.read_bytes() == before
    assert entry.session.title == ""
    assert not titles.pinned(registry_db, SESSION)
    assert title_events(frames) == []
    assert not entry.lock.locked()
    registry_db.close()
