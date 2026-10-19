"""Reading a Claude transcript: the settings it records, and the blocks it holds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts
from rc_client.agents.claude.holders import HolderScan
from rc_client.agents.claude.transcripts import TranscriptInfo, TranscriptTailer
from rc_client.agents.codex import rollouts
from rc_client.agents.grok import sessions as grok_sessions
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import ClaudeMirror, MirrorService


def model_row(model_id: str) -> dict[str, Any]:
    attachment = {"type": "model", "identity": {"modelId": model_id}}
    return {"type": "attachment", "attachment": attachment}


def effort_row(effort: str) -> dict[str, Any]:
    message = {"id": "msg_0", "content": [{"type": "text", "text": "ok"}]}
    return {"type": "assistant", "uuid": "a0", "effort": effort, "message": message}


def block_row(uuid: str, index: int, block: dict[str, Any]) -> dict[str, Any]:
    """One block of message `msg_1`, filed as a row of its own as Claude Code does."""
    message = {"id": "msg_1", "content": [block], "stop_reason": "end_turn"}
    return {"type": "assistant", "uuid": uuid, "apiBlockIndex": index, "message": message}


THOUGHT = block_row("a1", 0, {"type": "thinking", "thinking": "The test needs a fixture."})
ANSWER = block_row("a2", 1, {"type": "text", "text": "Added the fixture."})


def write_lines(path: Path, lines: list[bytes]) -> None:
    with path.open("ab") as handle:
        for line in lines:
            handle.write(line + b"\n")


def encoded(row: dict[str, Any]) -> bytes:
    return json.dumps(row).encode("utf-8")


class Mirrored:
    """A hub with mirrored Claude sessions, and nothing on this machine to find."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.frames: list[dict[str, Any]] = []
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        async def no_terminal() -> HolderScan:
            return HolderScan(holders=[], complete=True)

        self.found: list[TranscriptInfo] = []
        monkeypatch.setattr(transcripts, "discover", lambda *args: self.found)
        monkeypatch.setattr(rollouts, "discover", lambda *args: [])
        monkeypatch.setattr(grok_sessions, "discover", lambda *args: [])
        monkeypatch.setattr(mirror_module, "scan_holders", no_terminal)
        self.hub = SessionHub(self.registry, publish, "dev-1", lambda: [])
        self.mirror = MirrorService(self.hub)

    def transcript(self, session_id: str, lines: list[bytes]) -> Path:
        path = self.tmp_path / f"{session_id}.jsonl"
        write_lines(path, lines)
        stat = path.stat()
        self.found.append(
            TranscriptInfo(
                session_id=session_id,
                path=str(path),
                cwd=str(self.tmp_path),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        )
        # Read before; only the whole-file settings read sees what came first.
        self.registry.set_kv(self.mirror._offset_key(session_id), str(stat.st_size))
        return path

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    async def close(self) -> None:
        await self.hub.close()
        self.registry.close()


# ------------------------------------------------------------------ settings


def test_settings_are_read_past_a_row_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_lines(
        path,
        [encoded(model_row("claude-fable-5-1")), b'{"type": "user", "x": "\xff\xfe"}', b"\xc3("],
    )
    write_lines(path, [encoded(effort_row("high"))])
    assert transcripts.latest_settings(path) == {"model": "claude-fable-5-1", "effort": "high"}


async def test_a_settings_read_that_failed_is_tried_again_and_stops_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirrored = Mirrored(tmp_path, monkeypatch)
    failing = mirrored.transcript("first", [encoded(model_row("claude-opus-5"))])
    mirrored.transcript("second", [encoded(model_row("claude-fable-5-1"))])
    read = transcripts.latest_settings
    failures = [str(failing)]

    def flaky(path: str | Path, limit: int = transcripts.SETTINGS_SCAN_BYTES) -> dict[str, str]:
        if str(path) in failures:
            failures.remove(str(path))
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return read(path, limit)

    monkeypatch.setattr(transcripts, "latest_settings", flaky)

    await mirrored.mirror.scan_once()
    assert mirrored.hub.entry("first").session.model is None
    assert mirrored.hub.entry("second").session.model == "claude-fable-5-1"

    await mirrored.mirror.scan_once()
    assert mirrored.hub.entry("first").session.model == "claude-opus-5"
    await mirrored.close()


# -------------------------------------------------------------------- blocks


def block_ids(tailer: TranscriptTailer, rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (emit.kind, str(emit.fields["block_id"]))
        for row in rows
        for emit in tailer.translate(row)
        if "block_id" in emit.fields
    ]


def test_a_thought_and_the_answer_after_it_are_blocks_of_their_own() -> None:
    found = block_ids(TranscriptTailer(path="/nonexistent", cwd="/repo"), [THOUGHT, ANSWER])
    assert [kind for kind, _ in found] == ["thinking", "assistant_text"]
    assert len({block_id for _, block_id in found}) == 2
    # Read again, as after a restart, the same rows name the same blocks.
    assert block_ids(TranscriptTailer(path="/nonexistent", cwd="/repo"), [THOUGHT, ANSWER]) == (
        found
    )


def test_a_message_in_one_row_still_numbers_its_blocks_by_place() -> None:
    whole = {
        "type": "assistant",
        "uuid": "a1",
        "message": {
            "id": "msg_2",
            "content": [
                {"type": "thinking", "thinking": "First."},
                {"type": "text", "text": "Then this."},
            ],
            "stop_reason": "end_turn",
        },
    }
    found = block_ids(TranscriptTailer(path="/nonexistent", cwd="/repo"), [whole])
    assert [block_id for _, block_id in found] == ["msg_2:0", "msg_2:1"]


async def test_the_visible_thought_of_a_mirrored_session_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirrored = Mirrored(tmp_path, monkeypatch)
    entry = mirrored.hub.register_mirrored(
        Session(
            session_id="live",
            device_id="dev-1",
            agent="claude",
            cwd=str(tmp_path),
            origin="terminal",
            control="terminal",
        )
    )
    path = tmp_path / "live.jsonl"
    write_lines(path, [encoded(THOUGHT), encoded(ANSWER)])
    mirrored.mirror._claude["live"] = ClaudeMirror(
        tailer=TranscriptTailer(path=str(path), cwd=str(tmp_path))
    )

    await mirrored.mirror.tail_once()

    [thought] = mirrored.events("thinking")
    [answer] = mirrored.events("assistant_text")
    assert thought["block_id"] != answer["block_id"]
    kept = mirrored.registry.block(entry.session.session_id, thought["block_id"])
    assert kept is not None and kept["kind"] == "thinking"
    await mirrored.close()
