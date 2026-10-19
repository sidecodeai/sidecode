"""A49: a session whose first message is files alone is named after the files.

The agent's own summary of such a message can only be made of the paths it was
given, so it waits until the person has said something. Wherever an agent's
words quote a path to an attachment, written however the agent likes, the
apps get the file's name instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rc_client import attachments
from rc_client.agents.claude import transcripts
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions import titles
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import ClaudeMirror, MirrorService
from tests.test_hub import add_session, build_hub
from tests.test_shared_control import Harness

SHOT = {"name": "shot.png", "mime": "image/png", "data_base64": "aGVsbG8="}


def build_channel(tmp_path: Path, session_id: str = "s1") -> SessionChannel:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id=session_id, device_id="d1", agent="claude", cwd="/repo")
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        return None

    return SessionChannel(registry, session, publish)


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def user_row(text: str, uuid: str) -> dict[str, Any]:
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def title_row(title: str) -> dict[str, Any]:
    return {"type": "ai-title", "aiTitle": title, "sessionId": "sess-1"}


# --------------------------------------------------------------------- paths


def test_a_path_to_an_attachment_comes_off_however_it_was_written() -> None:
    [own] = attachments.materialise("sess-1", [SHOT])
    stored = "/".join(Path(own.path).parts[-4:])
    assert stored.startswith("attachments/sess-1/")

    assert attachments.without_paths(f"Review {own.path}") == "Review shot.png"
    assert attachments.without_paths(f"Review ~/.rc-client/state/{stored} now") == (
        "Review shot.png now"
    )
    assert attachments.without_paths(f"see `{stored}`.") == "see `shot.png`."
    assert attachments.without_paths(f"(/Volumes/Home/me/.rc-client/state/{stored})") == (
        "(shot.png)"
    )


def test_paths_that_only_look_like_an_attachment_s_are_left_alone() -> None:
    untouched = (
        "docs/attachments/guide.md, attachments/notes/today.txt and "
        "my-attachments/sess-1/0123456789ab/plan.png"
    )
    assert attachments.without_paths(untouched) == untouched


# -------------------------------------------------------------------- titles


async def test_a_message_of_files_alone_names_the_session_after_them(tmp_path: Path) -> None:
    channel = build_channel(tmp_path)
    files = [{"name": "shot.png"}, {"name": "notes.txt"}]
    assert await titles.from_prompt(channel, "", files) is True
    assert channel.session.title == "shot.png, notes.txt"

    # What the agent made of a prompt that was nothing but paths.
    assert await titles.from_agent(channel, "Rc-client state attachments review") is False
    assert channel.session.title == "shot.png, notes.txt"
    # A typed command says nothing about the conversation either.
    await titles.from_prompt(channel, "/compact")
    assert await titles.from_agent(channel, "Rc-client state attachments review") is False

    # The person's first words let the agent's summary in again.
    assert await titles.from_prompt(channel, "why does the login fail here?") is False
    assert await titles.from_agent(channel, "Login failure in a screenshot") is True
    assert channel.session.title == "Login failure in a screenshot"
    channel.registry.close()


async def test_words_with_files_name_the_session_after_the_words(tmp_path: Path) -> None:
    channel = build_channel(tmp_path)
    assert await titles.from_prompt(channel, "what is wrong here?", [{"name": "shot.png"}])
    assert channel.session.title == "what is wrong here?"
    assert await titles.from_agent(channel, "Login failure in a screenshot") is True
    channel.registry.close()


async def test_a_name_from_files_follows_the_session_to_its_own_id(tmp_path: Path) -> None:
    channel = build_channel(tmp_path, "temp-1")
    await titles.from_prompt(channel, "", [{"name": "shot.png"}])
    titles.rekey(channel.registry, "temp-1", "real-1")
    assert titles.named_after_files(channel.registry, "real-1") is True
    channel.registry.close()


# ------------------------------------------------------------- the two paths


async def test_a_terminal_session_started_with_files_alone_keeps_their_name(
    tmp_path: Path,
) -> None:
    """The transcript holds the prompt, paths and all, then the CLI's own title."""
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    entry = hub.register_mirrored(
        Session(
            session_id="sess-1",
            device_id="dev-1",
            agent="claude",
            cwd=str(tmp_path),
            origin="terminal",
            control="terminal",
        )
    )
    written = attachments.materialise("sess-1", [SHOT])
    path = tmp_path / "sess-1.jsonl"
    write_rows(path, [user_row(attachments.describe("", written), "u1")])
    mirror = MirrorService(hub)
    mirror._claude["sess-1"] = ClaudeMirror(
        tailer=transcripts.TranscriptTailer(path=str(path), cwd=str(tmp_path))
    )

    await mirror.tail_once()
    assert entry.session.title == "shot.png"
    write_rows(path, [title_row("Review rc-client state attachment shot")])
    await mirror.tail_once()
    assert entry.session.title == "shot.png"

    write_rows(path, [user_row("why is the button grey?", "u2"), title_row("Grey button")])
    await mirror.tail_once()
    assert entry.session.title == "Grey button"
    await hub.close()
    registry.close()


async def test_files_sent_alone_to_a_session_the_device_drives_name_it(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "", "attachments": [SHOT]})
    assert entry.session.title == "shot.png"
    assert await titles.from_agent(entry.channel, "Rc-client state attachments review") is False
    registry.close()


async def test_files_sent_alone_to_an_attached_terminal_name_its_session(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    entry = await harness.attach()
    message = {"id": "req-1", "session_id": "sess-1", "text": "", "attachments": [SHOT]}
    await harness.hub.send(message)
    assert entry.session.title == "shot.png"
    harness.close()
