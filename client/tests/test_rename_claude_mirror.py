"""A55: what the mirror makes of the rows that name a session, and what they do not move."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from rc_client.agents.claude import transcripts
from rc_client.registry import Registry
from rc_client.sessions import titles
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.mirror import ClaudeMirror, MirrorService
from tests.rename_helpers import summaries
from tests.test_hub import add_session, build_hub
from tests.test_rename_claude_rows import CAVEAT, COMMAND, NOTE, STDOUT
from tests.test_typist import Harness

SESSION = "5f8f6a02-1c9d-4a1e-9d4a-2f1e4c7b90aa"
LEDGER = "Ledger migration"
STAMP = 1_000

CUSTOM_TITLE = {"type": "custom-title", "customTitle": LEDGER, "sessionId": SESSION}
AGENT_NAME = {"type": "agent-name", "agentName": LEDGER, "sessionId": SESSION}
AI_TITLE = {"type": "ai-title", "aiTitle": "Create hello.txt file", "sessionId": SESSION}
WORDS = {"type": "user", "uuid": "w1", "message": {"role": "user", "content": "carry on"}}


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class Mirrored(NamedTuple):
    entry: SessionEntry
    mirror: MirrorService
    path: Path
    frames: list[dict[str, Any]]
    registry: Registry


async def mirrored(tmp_path: Path) -> Mirrored:
    """A session a terminal holds, mirrored from its transcript, read up to now."""
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    entry.runner = None
    entry.session.origin = "terminal"
    entry.session.control = "terminal"
    path = tmp_path / "terminal.jsonl"
    # One finished turn, so the session is idle when the test begins.
    write_rows(
        path,
        [
            {"type": "user", "uuid": "u0", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "uuid": "a0",
                "message": {
                    "id": "m0",
                    "content": [{"type": "text", "text": "hello"}],
                    "stop_reason": "end_turn",
                },
            },
        ],
    )
    mirror = MirrorService(hub)
    mirror._claude["sess-1"] = ClaudeMirror(
        tailer=transcripts.TranscriptTailer(path=str(path), cwd=str(tmp_path))
    )
    await mirror.tail_once()
    entry.session.updated_at = STAMP
    frames.clear()
    return Mirrored(entry, mirror, path, frames, registry_db)


@pytest.mark.parametrize(
    "rows",
    [[CUSTOM_TITLE], [AGENT_NAME], [CUSTOM_TITLE, AGENT_NAME]],
    ids=["title", "prompt-bar-name", "both"],
)
async def test_a_pass_that_reads_only_naming_rows_keeps_the_session_in_its_place(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> None:
    seen = await mirrored(tmp_path)

    write_rows(seen.path, rows)
    await seen.mirror.tail_once()

    assert seen.entry.session.updated_at == STAMP
    assert {summary["updated_at"] for summary in summaries(seen.frames)} <= {STAMP}
    if CUSTOM_TITLE in rows:
        assert seen.entry.session.title == LEDGER
        assert titles.pinned(seen.registry, "sess-1")
    seen.registry.close()


@pytest.mark.parametrize(
    "rows",
    [[WORDS], [AI_TITLE], [WORDS, CUSTOM_TITLE], [CUSTOM_TITLE, AI_TITLE]],
    ids=["a-message", "a-generated-title", "a-message-and-a-name", "a-name-and-a-generated-title"],
)
async def test_any_other_row_moves_the_session_as_it_always_did(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> None:
    """Generated titles keep today's behaviour: only a pass of nothing but names is quiet."""
    seen = await mirrored(tmp_path)

    write_rows(seen.path, rows)
    await seen.mirror.tail_once()

    assert seen.entry.session.updated_at > STAMP
    seen.registry.close()


async def test_a_name_the_cli_re_stamps_is_followed_without_moving_the_session(
    tmp_path: Path,
) -> None:
    """The CLI re-appends its cached metadata every few transcript writes and at exit."""
    seen = await mirrored(tmp_path)
    write_rows(seen.path, [CUSTOM_TITLE, AGENT_NAME])
    await seen.mirror.tail_once()

    write_rows(seen.path, [CUSTOM_TITLE, AGENT_NAME])
    await seen.mirror.tail_once()

    assert (seen.entry.session.title, seen.entry.session.updated_at) == (LEDGER, STAMP)
    seen.registry.close()


async def test_a_session_the_device_drives_follows_the_name_its_cli_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", tmp_path)
    project = tmp_path / "-repo"
    project.mkdir()
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.rekey(entry, SESSION)
    entry.session.origin = "remote"
    entry.session.updated_at = STAMP
    path = project / f"{SESSION}.jsonl"
    write_rows(path, [WORDS])
    mirror = MirrorService(hub)
    await mirror._watch_titles()
    frames.clear()

    write_rows(path, [CUSTOM_TITLE, AGENT_NAME])
    await mirror._read_titles()

    assert (entry.session.title, entry.session.updated_at) == (LEDGER, STAMP)
    assert {summary["updated_at"] for summary in summaries(frames)} == {STAMP}
    registry_db.close()


async def test_a_generated_title_never_confirms_a_rename_the_device_typed(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    entry = await harness.attach()
    mirror = MirrorService(harness.hub)
    pending = harness.hub.shared.expect(entry, "/rename", by_title=True)

    await mirror._apply_title(entry, transcripts.Title("Create hello.txt file", by_user=False))
    assert pending.done is not None and not pending.done.done()

    await mirror._apply_title(entry, transcripts.Title(LEDGER, by_user=True))
    assert pending.done.result() == LEDGER
    harness.close()


# ----------------------------------------------- a rename the person types in the terminal

PERSONS_RENAMES = [
    [COMMAND, CUSTOM_TITLE, AGENT_NAME, STDOUT, NOTE],
    [COMMAND, CUSTOM_TITLE, AGENT_NAME, NOTE, STDOUT],
    [COMMAND, NOTE, CUSTOM_TITLE, AGENT_NAME, STDOUT],
    [CAVEAT, COMMAND, STDOUT, CUSTOM_TITLE, AGENT_NAME, NOTE],
    [COMMAND, CUSTOM_TITLE, AGENT_NAME],
]
RENAME_IDS = [
    "output-then-note",
    "note-then-output",
    "note-first",
    "with-the-caveat",
    "titles-only",
]


def said(frames: list[dict[str, Any]]) -> list[str]:
    """The words the apps were shown as messages."""
    return [
        frame["event"]["text"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]


def turns(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "turn_started"
    ]


@pytest.mark.parametrize("rows", PERSONS_RENAMES, ids=RENAME_IDS)
async def test_a_rename_the_person_types_in_the_terminal_keeps_the_session_in_its_place(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> None:
    seen = await mirrored(tmp_path)

    write_rows(seen.path, rows)
    await seen.mirror.tail_once()

    assert seen.entry.session.updated_at == STAMP
    assert {summary["updated_at"] for summary in summaries(seen.frames)} <= {STAMP}
    assert seen.entry.session.title == LEDGER
    assert titles.pinned(seen.registry, "sess-1")
    # The command is the person's own words, as every typed command is (A32), and the
    # note for the model is nobody's: nothing is running afterwards.
    assert said(seen.frames) == ["/rename Ledger migration"]
    assert turns(seen.frames) == []
    assert seen.entry.session.state == "readonly"
    seen.registry.close()


async def test_the_rows_of_that_rename_read_in_separate_passes_are_all_quiet(
    tmp_path: Path,
) -> None:
    seen = await mirrored(tmp_path)

    for rows in ([CAVEAT, COMMAND], [CUSTOM_TITLE, AGENT_NAME], [STDOUT], [NOTE]):
        write_rows(seen.path, rows)
        await seen.mirror.tail_once()
        assert seen.entry.session.updated_at == STAMP, rows

    assert seen.entry.session.title == LEDGER
    seen.registry.close()


@pytest.mark.parametrize(
    "rows",
    [
        [COMMAND, CUSTOM_TITLE, AGENT_NAME, STDOUT, WORDS],
        [WORDS, COMMAND, CUSTOM_TITLE],
        [COMMAND, CUSTOM_TITLE, AI_TITLE],
    ],
    ids=["a-message-after", "a-message-before", "a-generated-title-too"],
)
async def test_a_pass_with_anything_else_in_it_still_moves_the_session(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> None:
    seen = await mirrored(tmp_path)

    write_rows(seen.path, rows)
    await seen.mirror.tail_once()

    assert seen.entry.session.updated_at > STAMP
    seen.registry.close()


async def test_the_answer_to_some_other_command_still_moves_the_session(tmp_path: Path) -> None:
    seen = await mirrored(tmp_path)

    write_rows(seen.path, [STDOUT])
    await seen.mirror.tail_once()

    assert seen.entry.session.updated_at > STAMP
    seen.registry.close()


async def test_a_rename_typed_in_a_terminal_the_device_is_attached_to_moves_nothing_either(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    path = tmp_path / "attached.jsonl"
    mirror = MirrorService(harness.hub)
    harness.hub.shared.reader = mirror
    entry = await harness.attach()
    mirror._claude["sess-1"] = ClaudeMirror(
        tailer=transcripts.TranscriptTailer(path=str(path), cwd=str(tmp_path))
    )
    entry.session.updated_at = STAMP
    harness.frames.clear()

    write_rows(path, [COMMAND, CUSTOM_TITLE, AGENT_NAME, STDOUT, NOTE])
    await mirror.tail_once()

    assert entry.session.updated_at == STAMP
    assert entry.session.title == LEDGER
    assert said(harness.frames) == ["/rename Ledger migration"]
    assert turns(harness.frames) == []
    assert entry.shared is not None and not entry.shared.running
    harness.close()
