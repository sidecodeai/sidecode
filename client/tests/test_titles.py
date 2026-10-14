"""Session titles: precedence, and the two Claude paths that carry one."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts
from rc_client.models import AgentInfo, Session, title_from_message, title_from_text
from rc_client.registry import Registry
from rc_client.sessions import titles
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.mirror import ClaudeMirror, MirrorService
from tests.test_hub import FakeRunner, add_session, build_hub

SESSION = "5f8f6a02-1c9d-4a1e-9d4a-2f1e4c7b90aa"


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def title_row(title: str, session_id: str = SESSION) -> dict[str, Any]:
    """The row Claude Code writes when it has named a session for itself."""
    return {"type": "ai-title", "aiTitle": title, "sessionId": session_id}


def rename_row(title: str, session_id: str = SESSION) -> dict[str, Any]:
    """The row `/rename` writes, byte for byte what the CLI's own writer emits."""
    return {"type": "custom-title", "customTitle": title, "sessionId": session_id}


def user_row(text: str, uuid: str = "u1") -> dict[str, Any]:
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def build_channel(tmp_path: Path) -> tuple[SessionChannel, Registry, list[dict[str, Any]]]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd="/repo")
    registry.upsert_session(session)
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    return SessionChannel(registry, session, publish), registry, frames


# ------------------------------------------------------------------ precedence


async def test_the_first_prompt_names_a_session_that_has_no_title(tmp_path: Path) -> None:
    channel, registry, frames = build_channel(tmp_path)
    assert await titles.from_prompt(channel, "fix the flaky auth test\nand rerun it") is True
    assert channel.session.title == "fix the flaky auth test"
    assert await titles.from_prompt(channel, "a later message") is False
    assert [frame["event"]["title"] for frame in frames if frame["type"] == "session.event"] == [
        "fix the flaky auth test"
    ]
    registry.close()


def test_a_slash_command_is_never_a_title_but_a_typed_one_is_kept() -> None:
    """Amendment A32: a typed command is a message, and still names nothing."""
    assert title_from_message("/compact") == ""
    assert title_from_message("  /model haiku\nand then rerun") == ""
    assert title_from_message("fix the flaky auth test") == "fix the flaky auth test"
    # A title the person typed goes the other way and is taken as given.
    assert title_from_text("/tmp cleanup") == "/tmp cleanup"


async def test_a_typed_command_leaves_a_session_unnamed(tmp_path: Path) -> None:
    channel, registry, _ = build_channel(tmp_path)
    assert await titles.from_prompt(channel, "/compact") is False
    assert channel.session.title == ""
    assert await titles.from_prompt(channel, "now fix the flaky test") is True
    assert channel.session.title == "now fix the flaky test"
    registry.close()


async def test_an_agent_title_replaces_the_first_prompt(tmp_path: Path) -> None:
    channel, registry, _ = build_channel(tmp_path)
    await titles.from_prompt(channel, "please look at hello.txt")
    assert await titles.from_agent(channel, "Create hello.txt file") is True
    assert channel.session.title == "Create hello.txt file"
    registry.close()


async def test_a_changed_agent_title_replaces_the_previous_one(tmp_path: Path) -> None:
    channel, registry, _ = build_channel(tmp_path)
    await titles.from_agent(channel, "First reading")
    assert await titles.from_agent(channel, "First reading") is False
    assert await titles.from_agent(channel, "Second reading") is True
    assert channel.session.title == "Second reading"
    registry.close()


async def test_a_title_the_user_set_is_never_overwritten(tmp_path: Path) -> None:
    channel, registry, _ = build_channel(tmp_path)
    assert await titles.from_user(channel, "Ledger migration") is True
    assert await titles.from_agent(channel, "Create hello.txt file") is False
    assert await titles.from_prompt(channel, "anything") is False
    assert channel.session.title == "Ledger migration"
    registry.close()


async def test_every_source_is_capped_at_the_protocol_length(tmp_path: Path) -> None:
    channel, registry, _ = build_channel(tmp_path)
    await titles.from_agent(channel, "x" * 200)
    assert len(channel.session.title) == 60
    assert channel.session.title.endswith("…")
    registry.close()


async def test_an_empty_title_changes_nothing(tmp_path: Path) -> None:
    channel, registry, _ = build_channel(tmp_path)
    await titles.from_prompt(channel, "keep me")
    assert await titles.from_agent(channel, "   ") is False
    assert channel.session.title == "keep me"
    registry.close()


def test_a_pin_follows_a_session_to_the_agents_own_id(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    titles.pin(registry, "provisional")
    titles.rekey(registry, "provisional", "thread-1")
    assert titles.pinned(registry, "thread-1") is True
    titles.rekey(registry, "unpinned", "thread-2")
    assert titles.pinned(registry, "thread-2") is False
    registry.close()


async def test_a_title_set_through_the_api_sticks_across_a_rekey(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.set_options({"session_id": "sess-1", "title": "Ledger migration"})
    await hub.rekey(entry, "real-id")
    await titles.from_agent(entry.channel, "Something the agent thought of")
    assert entry.session.title == "Ledger migration"
    registry.close()


async def test_a_created_session_keeps_the_title_it_was_given(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)

    async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
        return FakeRunner(entry.channel)

    hub._build_runner = build  # type: ignore[method-assign]
    created = await hub.create(
        {"agent": "claude", "cwd": str(tmp_path), "title": "Ledger migration"}
    )
    entry = hub.entry(created["session"]["session_id"])
    await titles.from_agent(entry.channel, "Something the agent thought of")
    assert entry.session.title == "Ledger migration"
    await hub.close()
    registry.close()


async def test_a_created_session_without_a_title_is_named_by_its_first_prompt(
    tmp_path: Path,
) -> None:
    hub, _, registry = build_hub(tmp_path)

    async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
        return FakeRunner(entry.channel)

    hub._build_runner = build  # type: ignore[method-assign]
    created = await hub.create(
        {"agent": "claude", "cwd": str(tmp_path), "first_message": "please look at hello.txt"}
    )
    entry = hub.entry(created["session"]["session_id"])
    assert entry.session.title == "please look at hello.txt"
    assert await titles.from_agent(entry.channel, "Create hello.txt file") is True
    await hub.close()
    registry.close()


# ------------------------------------------------------------------ transcripts


def test_both_title_rows_are_read_and_anything_else_is_not() -> None:
    assert transcripts.read_title(title_row("Create hello.txt file")) == transcripts.Title(
        "Create hello.txt file", by_user=False
    )
    assert transcripts.read_title(rename_row("Ledger migration")) == transcripts.Title(
        "Ledger migration", by_user=True
    )
    assert transcripts.read_title(user_row("hello")) is None


def test_a_title_row_the_format_changed_under_is_ignored_not_fatal() -> None:
    """The transcript format is internal to Claude Code and may change."""
    for row in (
        {"type": "ai-title"},
        {"type": "ai-title", "aiTitle": "  "},
        {"type": "ai-title", "aiTitle": {"text": "an object now"}},
        {"type": "custom-title", "title": "renamed field"},
        {"type": "custom-title", "customTitle": None},
        {},
    ):
        assert transcripts.read_title(row) is None
        assert transcripts.TranscriptTailer(path="/dev/null", cwd="/repo").translate(row) == []


def test_the_title_tail_returns_every_title_in_order(tmp_path: Path) -> None:
    path = tmp_path / f"{SESSION}.jsonl"
    write_rows(path, [user_row("hello"), title_row("First reading")])
    tail = transcripts.TitleTail(path=str(path))
    assert tail.read_new() == [transcripts.Title("First reading", by_user=False)]
    assert tail.read_new() == []
    write_rows(path, [rename_row("Ledger migration"), title_row("Second reading")])
    assert tail.read_new() == [
        transcripts.Title("Ledger migration", by_user=True),
        transcripts.Title("Second reading", by_user=False),
    ]


def test_a_transcript_is_found_by_session_id_across_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    project = tmp_path / "-Users-me-dev-gateway"
    project.mkdir()
    write_rows(project / f"{SESSION}.jsonl", [user_row("hello")])
    found = transcripts.find_transcript(SESSION)
    assert found is not None and found.name == f"{SESSION}.jsonl"
    assert transcripts.find_transcript("00000000-0000-0000-0000-000000000000") is None
    assert transcripts.find_transcript("../../etc/passwd") is None


def test_the_tailer_turns_a_title_row_into_an_internal_emit() -> None:
    tailer = transcripts.TranscriptTailer(path="/dev/null", cwd="/repo")
    generated = tailer.translate(title_row("Create hello.txt file"))
    assert [(emit.kind, emit.fields) for emit in generated] == [
        (transcripts.TITLE, {"title": "Create hello.txt file", "by_user": False})
    ]
    renamed = tailer.translate(rename_row("Ledger migration"))
    assert renamed[0].fields == {"title": "Ledger migration", "by_user": True}


# ----------------------------------------------------------------- the mirror


async def test_a_mirrored_terminal_session_takes_the_title_from_its_transcript(
    tmp_path: Path,
) -> None:
    hub, frames, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = None
    entry.session.origin = "terminal"
    entry.session.control = "terminal"
    path = tmp_path / "terminal.jsonl"
    write_rows(path, [user_row("please look at hello.txt")])
    mirror = MirrorService(hub)
    mirror._claude["sess-1"] = ClaudeMirror(
        tailer=transcripts.TranscriptTailer(path=str(path), cwd=str(tmp_path))
    )

    await mirror.tail_once()
    assert entry.session.title == "please look at hello.txt"

    write_rows(path, [title_row("Create hello.txt file")])
    await mirror.tail_once()
    assert entry.session.title == "Create hello.txt file"
    assert frames[-1]["type"] == "session.updated"

    # `/rename` in the terminal is the user speaking, and nothing generated
    # afterwards replaces it.
    write_rows(path, [rename_row("Ledger migration"), title_row("A later reading")])
    await mirror.tail_once()
    assert entry.session.title == "Ledger migration"
    registry.close()


async def test_a_session_this_device_drives_is_watched_for_its_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    project = tmp_path / "-repo"
    project.mkdir()
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.rekey(entry, SESSION)
    entry.session.origin = "remote"
    await titles.from_prompt(entry.channel, "please look at hello.txt")
    path = project / f"{SESSION}.jsonl"
    write_rows(path, [user_row("please look at hello.txt")])
    mirror = MirrorService(hub)

    await mirror._watch_titles()
    assert set(mirror._titles) == {SESSION}
    write_rows(path, [title_row("Create hello.txt file")])
    await mirror._read_titles()
    assert entry.session.title == "Create hello.txt file"

    # A rename and a generated title in the same batch: the rename wins.
    write_rows(path, [rename_row("Ledger migration"), title_row("A later reading")])
    await mirror._read_titles()
    assert entry.session.title == "Ledger migration"

    # A session the device stops driving is no longer watched.
    entry.runner = None
    await mirror._watch_titles()
    assert mirror._titles == {}
    registry.close()


async def test_a_mirrored_session_is_not_watched_twice(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    entry.session.origin = "terminal"
    assert isinstance(entry.runner, FakeRunner)
    mirror = MirrorService(hub)
    await mirror._watch_titles()
    assert mirror._titles == {}
    registry.close()
