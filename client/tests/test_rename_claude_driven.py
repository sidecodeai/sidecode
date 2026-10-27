"""A55: renaming a Claude session the device drives, through the SDK's control request."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rc_client.agents.claude import name_rows, renaming, runtime
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.sessions import titles
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.rename_helpers import summaries, title_events
from tests.test_hub import build_hub

SESSION = "5f8f6a02-1c9d-4a1e-9d4a-2f1e4c7b90aa"
LEDGER = "Ledger migration"


class FakeQuery:
    """The SDK's private query object: records each control request and answers as told."""

    def __init__(
        self,
        *,
        error: str | None = None,
        effect: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.error = error
        self.effect = effect
        self.requests: list[tuple[dict[str, Any], float]] = []
        self.locked: list[bool] = []
        self.entry: SessionEntry | None = None

    async def _send_control_request(
        self, request: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        self.requests.append((request, timeout))
        if self.entry is not None:
            self.locked.append(self.entry.lock.locked())
        if self.error is not None:
            raise Exception(self.error)
        if self.effect is not None:
            self.effect(request)
        return {}


def project_transcript(rows: list[dict[str, Any]] | None = None) -> Path:
    """A transcript where Claude Code files one, under the scratch projects directory."""
    project = runtime.PROJECTS_DIR / "-repo"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{SESSION}.jsonl"
    body = "".join(json.dumps(row) + "\n" for row in rows or [user_row("hello")])
    path.write_text(body, encoding="utf-8")
    return path


def user_row(text: str) -> dict[str, Any]:
    return {"type": "user", "uuid": "u1", "message": {"role": "user", "content": text}}


def cli_writes(path: Path, name: str) -> Callable[[dict[str, Any]], None]:
    """What the CLI's own routine leaves in the transcript, both rows, under `name`."""

    def write(request: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            for row in (
                {"type": "custom-title", "customTitle": name, "sessionId": SESSION},
                {"type": "agent-name", "agentName": name, "sessionId": SESSION},
            ):
                handle.write(json.dumps(row) + "\n")

    return write


def row_types(path: Path) -> list[str]:
    return [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines()]


def driven(hub: SessionHub, client: object | None) -> SessionEntry:
    """A session this device drives, whose SDK client is `client`."""
    session = Session(
        session_id=SESSION,
        device_id="dev-1",
        agent="claude",
        cwd="/repo",
        state="idle",
        control="remote",
        updated_at=1_000,
    )
    hub.registry.upsert_session(session)
    channel = SessionChannel(hub.registry, session, hub.publish)
    entry = SessionEntry(session=session, channel=channel)
    runner = ClaudeRunner(channel, binary=None, cwd="/repo")
    runner._client = client  # type: ignore[assignment]
    entry.runner = runner
    hub.entries[SESSION] = entry
    return entry


def ask(title: str = LEDGER) -> dict[str, Any]:
    return {"session_id": SESSION, "title": title}


async def test_the_cli_is_asked_with_the_control_request_and_its_own_row_is_the_answer(
    tmp_path: Path,
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    path = project_transcript()
    query = FakeQuery(effect=cli_writes(path, f"{LEDGER} (2)"))
    entry = driven(hub, SimpleNamespace(_query=query))
    query.entry = entry

    result = await hub.set_options(ask())

    assert query.requests == [
        (
            {
                "subtype": "rename_session",
                "title": LEDGER,
                "source": "host",
                "session_id": SESSION,
            },
            10.0,
        )
    ]
    assert query.locked == [True]
    # The CLI suffixed the name another live session holds; the reply carries what it holds.
    assert result["session"]["title"] == f"{LEDGER} (2)"
    assert titles.pinned(registry_db, SESSION)
    assert title_events(frames) == [f"{LEDGER} (2)"]
    # A rename is not activity.
    assert result["session"]["updated_at"] == 1_000
    assert {summary["updated_at"] for summary in summaries(frames)} == {1_000}
    # The CLI's routine wrote both rows; the device wrote none of its own.
    assert row_types(path) == ["user", "custom-title", "agent-name"]
    registry_db.close()


async def test_the_request_names_the_session_the_cli_reported(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    query = FakeQuery(effect=cli_writes(path, LEDGER))
    entry = driven(hub, SimpleNamespace(_query=query))
    assert isinstance(entry.runner, ClaudeRunner)
    entry.runner._translator.session_id = "the-id-the-cli-reported"

    await hub.set_options(ask())

    assert query.requests[0][0]["session_id"] == "the-id-the-cli-reported"
    registry_db.close()


async def test_a_turn_under_way_does_not_stop_a_rename(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    query = FakeQuery(effect=cli_writes(path, LEDGER))
    entry = driven(hub, SimpleNamespace(_query=query))
    assert isinstance(entry.runner, ClaudeRunner)
    entry.runner._turn_done.clear()
    assert entry.runner.busy

    result = await hub.set_options(ask())

    assert result["session"]["title"] == LEDGER
    registry_db.close()


async def test_a_request_the_cli_refuses_is_written_as_the_title_row_alone(
    tmp_path: Path,
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    query = FakeQuery(error="session_id does not match the running session")
    driven(hub, SimpleNamespace(_query=query))

    result = await hub.set_options(ask())

    assert len(query.requests) == 1
    assert result["session"]["title"] == LEDGER
    # A CLI holds the session, which re-stamps an `agent-name` written under it.
    assert row_types(path) == ["user", "custom-title"]
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[1]) == {
        "type": "custom-title",
        "customTitle": LEDGER,
        "sessionId": SESSION,
    }
    registry_db.close()


async def test_the_fallback_row_does_not_cut_a_line_the_live_cli_may_be_writing(
    tmp_path: Path,
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    with path.open("ab") as handle:
        handle.write(b'{"type":"user","uuid":"in-flight","mess')
    before = path.read_bytes()
    driven(hub, SimpleNamespace(_query=FakeQuery(error="no")))

    result = await hub.set_options(ask())

    assert result["session"]["title"] == LEDGER
    row = {"type": "custom-title", "customTitle": LEDGER, "sessionId": SESSION}
    # The CLI that holds the file seals its own tail; ours goes on the end as it stands.
    assert (
        path.read_bytes()[len(before) :] == (json.dumps(row, separators=(",", ":")) + "\n").encode()
    )
    registry_db.close()


@pytest.mark.parametrize(
    "client",
    [
        None,
        SimpleNamespace(),
        SimpleNamespace(_query=None),
        SimpleNamespace(_query=SimpleNamespace()),
    ],
    ids=["no-client", "no-query-attribute", "no-query", "no-request-method"],
)
async def test_an_sdk_that_cannot_send_the_request_falls_back_to_the_row(
    tmp_path: Path, client: object | None
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    path = project_transcript()
    driven(hub, client)

    result = await hub.set_options(ask())

    assert result["session"]["title"] == LEDGER
    assert row_types(path) == ["user", "custom-title"]
    registry_db.close()


async def test_a_request_the_cli_took_but_left_no_row_for_is_written_by_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's own record is the truth: a name nobody can find in it is not held."""
    monkeypatch.setattr(renaming, "READBACK", 0.05)
    hub, _, registry_db = build_hub(tmp_path)
    older = {"type": "custom-title", "customTitle": "Before the request", "sessionId": SESSION}
    path = project_transcript([user_row("hello"), older])
    query = FakeQuery()
    driven(hub, SimpleNamespace(_query=query))

    result = await hub.set_options(ask())

    assert len(query.requests) == 1
    assert result["session"]["title"] == LEDGER
    assert row_types(path) == ["user", "custom-title", "custom-title"]
    registry_db.close()


async def test_a_session_with_no_transcript_yet_is_left_to_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(renaming, "READBACK", 0.05)
    hub, _, registry_db = build_hub(tmp_path)
    query = FakeQuery()
    driven(hub, SimpleNamespace(_query=query))

    result = await hub.set_options(ask())

    assert len(query.requests) == 1
    assert result["session"]["title"] == LEDGER
    assert not runtime.PROJECTS_DIR.exists()
    registry_db.close()


async def test_a_refused_request_and_no_transcript_is_unsupported_and_names_nothing(
    tmp_path: Path,
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = driven(hub, SimpleNamespace(_query=FakeQuery(error="no")))

    with pytest.raises(RcError) as caught:
        await hub.set_options(ask())

    assert (caught.value.code, caught.value.message) == ("unsupported", renaming.NO_TRANSCRIPT)
    assert entry.session.title == ""
    assert not titles.pinned(registry_db, SESSION)
    assert title_events(frames) == []
    assert not entry.lock.locked()
    assert not runtime.PROJECTS_DIR.exists()
    registry_db.close()


async def test_a_transcript_that_cannot_be_written_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    project_transcript()
    driven(hub, None)

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied", "/secret/path/transcript.jsonl")

    monkeypatch.setattr(name_rows, "append_names", refuse)

    with pytest.raises(RcError) as caught:
        await hub.set_options(ask())

    # The message leaves the device through the gateway: no path, no errno text.
    assert (caught.value.code, caught.value.message) == ("unsupported", renaming.UNWRITABLE)
    registry_db.close()
