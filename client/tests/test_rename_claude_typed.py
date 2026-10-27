"""A55: renaming a Claude session attached to a terminal, by typing `/rename` into it."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import runtime, transcripts
from rc_client.errors import RcError
from rc_client.sessions import titles, typist
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.mirror import ClaudeMirror, MirrorService
from rc_client.sessions.ptys import TerminalState
from rc_client.sessions.typist import TypedCommand
from tests.rename_helpers import summaries, title_events
from tests.test_typist import FakeTerminal, Harness

LEDGER = "Ledger migration"


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


async def _until(ready: Callable[[], object], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the device never got that far")


def ask(title: str = LEDGER) -> dict[str, Any]:
    return {"session_id": "sess-1", "title": title}


def project_transcript() -> Path:
    project = runtime.PROJECTS_DIR / "-repo"
    project.mkdir(parents=True, exist_ok=True)
    path = project / "sess-1.jsonl"
    path.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n", encoding="utf-8")
    return path


def row_types(path: Path) -> list[str]:
    return [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ the script


async def test_the_command_is_typed_with_the_name_and_the_clis_own_row_confirms_it(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    entry.session.updated_at = 1_000
    terminal = harness.terminal
    harness.frames.clear()

    rename = asyncio.create_task(harness.hub.set_options(ask("  Ledger \t migration\n")))
    await _until(lambda: terminal.typed)

    # The name went through the normaliser, and nothing is said until the CLI has written its row.
    assert terminal.typed == ["/rename Ledger migration\r"]
    assert not rename.done()
    assert entry.session.title == ""
    assert harness.hub.shared.renaming(entry)

    # What the transcript reader makes of the rows the CLI writes: the command's own, then
    # the name, which the CLI may have suffixed.
    assert await harness.hub.shared.typed_command(entry, f"/rename {LEDGER}") is True
    await harness.hub.shared.title_seen(entry, f"{LEDGER} (2)")
    result = await asyncio.wait_for(rename, timeout=2)

    assert result["session"]["title"] == f"{LEDGER} (2)"
    assert entry.session.title == f"{LEDGER} (2)"
    assert titles.pinned(harness.registry, "sess-1")
    assert title_events(harness.frames) == [f"{LEDGER} (2)"]
    # A rename is not a message, and it is not activity.
    assert harness.events("user_message") == []
    assert {summary["updated_at"] for summary in summaries(harness.frames)} == {1_000}
    assert terminal.typed == ["/rename Ledger migration\r"], "nothing else, Escape included"
    assert entry.shared is not None and entry.shared.typed is None
    # The rows that follow the confirmation are still the rename's own.
    assert harness.hub.shared.renaming(entry)
    assert await harness.hub.shared.typed_command(entry, f"/rename {LEDGER}") is True


async def test_a_repeated_request_for_the_name_the_session_holds_types_nothing(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    terminal = harness.terminal
    first = asyncio.create_task(harness.hub.set_options(ask()))
    await _until(lambda: terminal.typed)
    await harness.hub.shared.title_seen(entry, LEDGER)
    await asyncio.wait_for(first, timeout=2)

    again = await harness.hub.set_options(ask())

    assert again["session"]["title"] == LEDGER
    assert terminal.typed == [f"/rename {LEDGER}\r"]


async def test_the_transcript_is_asked_for_again_while_the_rename_waits(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(typist, "RENAME_POLL", 0.05)
    asked: list[int] = []

    class Reader:
        def read_soon(self) -> None:
            asked.append(1)

        def settle_soon(self, session_id: str, delay: float) -> None:
            return None

    harness.hub.shared.reader = Reader()
    entry = await harness.attach()
    terminal = harness.terminal
    rename = asyncio.create_task(harness.hub.set_options(ask()))
    await _until(lambda: terminal.typed)

    await asyncio.sleep(0.3)
    assert len(asked) >= 3, "the reader's own two-second pace would make every rename wait for it"

    await harness.hub.shared.title_seen(entry, LEDGER)
    await asyncio.wait_for(rename, timeout=2)


# ------------------------------------------------------------------ what refuses it


@pytest.mark.parametrize("name", ["two\rlines", "esc\x1b[A", " padded", "a  b", "", "x" * 61])
async def test_text_that_was_not_normalised_is_never_typed(harness: Harness, name: str) -> None:
    entry = await harness.attach()

    with pytest.raises(RcError) as caught:
        await typist.Typist(entry, harness.terminal, harness.hub.shared).rename(name)

    assert caught.value.code == "bad_request"
    assert harness.terminal.typed == []
    assert claimed(entry) is None


@pytest.mark.parametrize(
    "busy",
    [{"draft": 3}, {"idle_for": 0.5}],
    ids=["somebody-is-typing", "the-keyboard-was-just-used"],
)
async def test_a_busy_terminal_refuses_the_rename_and_nothing_is_typed(
    harness: Harness, busy: dict[str, Any]
) -> None:
    entry = await harness.attach(FakeTerminal(**busy))

    with pytest.raises(RcError) as caught:
        await harness.hub.set_options(ask())

    assert (caught.value.code, caught.value.message) == ("conflict", typist.BUSY)
    assert harness.terminal.typed == []
    assert entry.session.title == ""
    assert not titles.pinned(harness.registry, "sess-1")
    assert entry.shared is not None and entry.shared.typed is None
    assert not entry.lock.locked()


async def test_a_turn_under_way_refuses_the_rename_and_nothing_is_typed(harness: Harness) -> None:
    entry = await harness.attach()
    assert entry.shared is not None
    entry.shared.running = True

    with pytest.raises(RcError) as caught:
        await harness.hub.set_options(ask())

    assert (caught.value.code, caught.value.message) == ("conflict", typist.BUSY)
    assert harness.terminal.typed == []


async def test_a_rename_the_cli_never_confirms_is_a_timeout_and_presses_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(typist, "CONFIRM_TIMEOUT", 0.3)
    monkeypatch.setattr(typist, "RENAME_POLL", 0.05)
    entry = await harness.attach()

    with pytest.raises(RcError) as caught:
        await harness.hub.set_options(ask())

    assert (caught.value.code, caught.value.message) == ("timeout", typist.NOT_CONFIRMED)
    # The keys may have landed, and no dialog was opened to close: no Escape.
    assert harness.terminal.typed == [f"/rename {LEDGER}\r"]
    assert entry.session.title == ""
    assert not titles.pinned(harness.registry, "sess-1")
    assert entry.shared is not None and entry.shared.typed is None
    assert not entry.lock.locked()


async def test_the_terminal_going_away_mid_rename_is_a_timeout(harness: Harness) -> None:
    entry = await harness.attach()
    terminal = harness.terminal
    rename = asyncio.create_task(harness.hub.set_options(ask()))
    await _until(lambda: terminal.typed)

    assert entry.shared is not None
    harness.hub.shared._drop_typed(entry.shared)

    with pytest.raises(RcError) as caught:
        await asyncio.wait_for(rename, timeout=2)
    assert (caught.value.code, caught.value.message) == ("timeout", typist.NOT_CONFIRMED)


async def test_a_terminal_the_device_does_not_own_the_keyboard_of_is_unsupported(
    harness: Harness,
) -> None:
    """Attached by the channel but started without the shim's proxy: no keystroke can reach it."""
    await harness.hub.attach_registered(harness.attachment)
    entry = harness.hub.entry("sess-1")

    for title in (LEDGER, "ask @alice"):
        with pytest.raises(RcError) as caught:
            await harness.hub.set_options(ask(title))
        assert (caught.value.code, caught.value.message) == ("unsupported", typist.NOT_TYPEABLE)

    assert entry.session.title == ""
    assert not entry.lock.locked()


# ------------------------------------------------------------------ names that are not typed


@pytest.mark.parametrize("name", ["ask @alice", "see @docs/ARCH.md", "a path C:\\", "ends with \\"])
async def test_a_name_the_prompt_box_would_misread_is_written_and_never_typed(
    harness: Harness, name: str
) -> None:
    entry = await harness.attach()
    entry.session.updated_at = 1_000
    path = project_transcript()

    result = await harness.hub.set_options(ask(name))

    assert harness.terminal.typed == []
    assert result["session"]["title"] == name
    assert titles.pinned(harness.registry, "sess-1")
    assert result["session"]["updated_at"] == 1_000
    # The title row alone, as Anthropic's own `/resume` picker writes it: the CLI absorbs
    # it, and re-stamps an `agent-name` written under it.
    assert row_types(path) == ["user", "custom-title"]


async def test_such_a_name_does_not_wait_for_an_idle_terminal(harness: Harness) -> None:
    """Nothing is typed, so there is nobody to be busy with."""
    entry = await harness.attach(FakeTerminal(draft=3))
    project_transcript()

    result = await harness.hub.set_options(ask("ask @alice"))

    assert result["session"]["title"] == "ask @alice"
    assert harness.terminal.typed == []
    assert entry.shared is not None and entry.shared.typed is None


# ------------------------------------------------------------------ the rows it leaves


async def test_a_command_row_nobody_claimed_is_the_persons_own(harness: Harness) -> None:
    entry = await harness.attach()

    # No rename is under way, so a `/rename` in the transcript was typed by the person,
    # and is published as a terminal message like any typed command (A32).
    assert await harness.hub.shared.typed_command(entry, f"/rename {LEDGER}") is False

    # Within a rename's window the row is the device's, whichever of its rows is read first.
    harness.hub.shared.quiet_for(entry, 5.0)
    assert await harness.hub.shared.typed_command(entry, f"/rename {LEDGER}") is True
    assert await harness.hub.shared.typed_command(entry, "/model opus") is False

    harness.hub.shared.quiet_for(entry, -1.0)
    assert await harness.hub.shared.typed_command(entry, f"/rename {LEDGER}") is False


def claimed(entry: SessionEntry) -> TypedCommand | None:
    return entry.shared.typed if entry.shared is not None else None


async def test_a_title_row_nobody_waits_for_confirms_nothing(harness: Harness) -> None:
    entry = await harness.attach()

    await harness.hub.shared.title_seen(entry, LEDGER)
    assert claimed(entry) is None

    # A command the device typed that is not a rename is not waiting for a name.
    pending = harness.hub.shared.expect(entry, "/model", "set model")
    await harness.hub.shared.title_seen(entry, LEDGER)
    assert claimed(entry) is pending
    assert pending.done is not None and not pending.done.done()


# ------------------------------------------------------------------ the whole exchange


def command_row(name: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": "c1",
        "message": {
            "role": "user",
            "content": "<command-name>/rename</command-name>\n"
            f"<command-message>rename</command-message>\n<command-args>{name}</command-args>",
        },
    }


def stdout_row(effective: str) -> dict[str, Any]:
    text = f"<local-command-stdout>Session renamed to: {effective}</local-command-stdout>"
    return {"type": "user", "uuid": "o1", "message": {"role": "user", "content": text}}


def note_row(effective: str) -> dict[str, Any]:
    text = f'The user named this session "{effective}". This may indicate the session\'s focus.'
    return {
        "type": "user",
        "uuid": "n1",
        "isMeta": True,
        "message": {"role": "user", "content": text},
    }


def title_rows(effective: str) -> list[dict[str, Any]]:
    return [
        {"type": "custom-title", "customTitle": effective, "sessionId": "sess-1"},
        {"type": "agent-name", "agentName": effective, "sessionId": "sess-1"},
    ]


class CliTerminal(FakeTerminal):
    """A terminal whose CLI answers `/rename` as the transcript says Claude Code does."""

    def __init__(self, respond: Callable[[str], Awaitable[None]]) -> None:
        super().__init__()
        self.respond = respond

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        state = await super().keys(data, clear)
        if data.startswith(f"{typist.RENAME_COMMAND} ") and data.endswith(typist.ENTER):
            await self.respond(data[len(typist.RENAME_COMMAND) + 1 : -1])
        return state


@pytest.mark.parametrize(
    "arrangement",
    ["titles-note-output", "note-titles-output", "titles-output-note", "titles-only"],
)
async def test_the_whole_exchange_through_the_mirror_leaves_the_session_where_it_was(
    harness: Harness, arrangement: str
) -> None:
    path = runtime.PROJECTS_DIR / "-repo" / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    mirror = MirrorService(harness.hub)
    harness.hub.shared.reader = mirror

    async def respond(name: str) -> None:
        """The CLI files its rows, which the mirror then reads, as it would a moment later."""
        effective = f"{name} (2)"
        order = {
            "titles-note-output": [
                *title_rows(effective),
                note_row(effective),
                stdout_row(effective),
            ],
            "note-titles-output": [
                note_row(effective),
                *title_rows(effective),
                stdout_row(effective),
            ],
            "titles-output-note": [
                *title_rows(effective),
                stdout_row(effective),
                note_row(effective),
            ],
            "titles-only": title_rows(effective),
        }[arrangement]
        with path.open("a", encoding="utf-8") as handle:
            for row in [command_row(name), *order]:
                handle.write(json.dumps(row) + "\n")
        await mirror.tail_once()

    entry: SessionEntry = await harness.attach(CliTerminal(respond))
    mirror._claude["sess-1"] = ClaudeMirror(
        tailer=transcripts.TranscriptTailer(path=str(path), cwd="/repo")
    )
    entry.session.updated_at = 1_000
    harness.frames.clear()

    result = await harness.hub.set_options(ask())

    assert harness.terminal.typed == [f"/rename {LEDGER}\r"]
    assert result["session"]["title"] == f"{LEDGER} (2)"
    assert titles.pinned(harness.registry, "sess-1")
    # Not a bubble for the command, not a turn for the note, and the row keeps its place.
    assert harness.events("user_message") == []
    assert harness.events("turn_started") == []
    assert entry.session.state != "running"
    assert entry.shared is not None and not entry.shared.running
    assert {summary["updated_at"] for summary in summaries(harness.frames)} == {1_000}
    assert entry.session.updated_at == 1_000


async def test_an_ordinary_row_after_the_rename_moves_the_session_as_it_always_did(
    harness: Harness,
) -> None:
    path = runtime.PROJECTS_DIR / "-repo" / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    mirror = MirrorService(harness.hub)
    harness.hub.shared.reader = mirror
    entry = await harness.attach()
    mirror._claude["sess-1"] = ClaudeMirror(
        tailer=transcripts.TranscriptTailer(path=str(path), cwd="/repo")
    )
    entry.session.updated_at = 1_000
    harness.hub.shared.quiet_for(entry, -1.0)

    words = {"type": "user", "uuid": "w1", "message": {"role": "user", "content": "carry on"}}
    path.write_text(json.dumps(words) + "\n", encoding="utf-8")
    await mirror.tail_once()

    assert entry.session.updated_at > 1_000
