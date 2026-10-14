"""Amendment A49: where a message's files are written, and how they stop being kept."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client import attachments
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.config import state_dir
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.helpers import drained
from tests.test_hub import FakeRunner, add_session, build_hub

SHOT = {"name": "shot.png", "mime": "image/png", "data_base64": "aGVsbG8="}
NOTES = {"name": "notes.txt", "mime": "text/plain", "data_base64": "aGk="}
DAY = 24 * 3600.0


def message_folders(session_id: str) -> list[Path]:
    folder = attachments.state_root() / session_id
    return sorted(folder.iterdir()) if folder.is_dir() else []


def aged(path: Path, days: float) -> Path:
    moment = time.time() - days * DAY
    os.utime(path, (moment, moment), follow_symlinks=False)
    return path


def planted(relative: str, size: int, days: float) -> Path:
    """A file in the root as some earlier message left it, `days` old."""
    path = attachments.state_root() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return aged(path, days)


# ------------------------------------------------------------------ layout


def test_each_message_gets_a_private_folder_of_its_own() -> None:
    first = attachments.materialise("sess-1", [SHOT])
    second = attachments.materialise("sess-1", [SHOT])

    assert first[0].path != second[0].path, "two photo.jpg never overwrite each other"
    folders = message_folders("sess-1")
    assert len(folders) == 2
    for folder in folders:
        assert len(folder.name) == 12
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    root = attachments.state_root()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "sess-1").stat().st_mode) == 0o700
    path = Path(first[0].path)
    assert (path.name, path.parent in folders) == ("shot.png", True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_two_files_of_one_name_in_one_message_are_both_kept() -> None:
    written = attachments.materialise("sess-1", [SHOT, SHOT, NOTES])
    assert [item.name for item in written] == ["shot.png", "shot-2.png", "notes.txt"]
    assert [Path(item.path).read_bytes() for item in written] == [b"hello", b"hello", b"hi"]
    assert len({Path(item.path).parent for item in written}) == 1


def test_a_name_keeps_the_extension_an_agent_tells_images_by() -> None:
    uploads = [
        {**SHOT, "name": "照片.jpg"},
        {**SHOT, "name": "截屏 2026-10-14 10.00.00.png"},
        {**SHOT, "name": "../../escape"},
        {**SHOT, "name": ""},
    ]
    written = attachments.materialise("sess-1", uploads)
    assert [item.name for item in written] == [
        "attachment-0.jpg",
        "2026-10-14_10.00.00.png",
        "escape",
        "attachment-3",
    ]
    assert all(Path(item.path).parent == Path(written[0].path).parent for item in written)


def test_a_message_with_one_bad_file_writes_nothing() -> None:
    with pytest.raises(RcError) as caught:
        attachments.materialise(
            "sess-1", [SHOT, {"name": "x", "mime": "text/plain", "data_base64": "!!!"}]
        )
    assert caught.value.code == "bad_request"
    assert message_folders("sess-1") == []


def test_a_session_id_never_names_a_folder_outside_the_root() -> None:
    written = attachments.materialise("..", [SHOT])
    root = attachments.state_root()
    assert Path(written[0].path).parent.parent.parent == root
    neighbour = root.parent / "rc-client.sqlite3"
    neighbour.write_bytes(b"state")

    attachments.cleanup("..")

    assert neighbour.exists(), "the state beside the root is never touched"
    assert root.is_dir(), "and neither is the root itself"
    assert not Path(written[0].path).exists()


def test_a_link_where_the_session_folder_belongs_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = attachments.state_root()
    root.mkdir(parents=True)
    (root / "sess-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RcError) as caught:
        attachments.materialise("sess-1", [SHOT])
    assert caught.value.code == "internal"
    assert list(outside.iterdir()) == []


# ----------------------------------------------------------- what is shown


def test_the_prompt_names_the_files_and_reads_back_as_the_typed_words() -> None:
    written = attachments.materialise("sess-1", [SHOT, NOTES])
    prompt = attachments.describe("look at these", written)
    assert prompt == (
        f"look at these\n\nAttached files:\n- shot.png: {written[0].path}\n"
        f"- notes.txt: {written[1].path}"
    )
    said, files = attachments.split_prompt(prompt)
    assert said == "look at these"
    assert files == [
        {"name": "shot.png", "mime": "image/png", "size": 5},
        {"name": "notes.txt", "mime": "text/plain", "size": 2},
    ]
    assert attachments.split_prompt(attachments.describe("", written)) == ("", files)


def test_a_file_already_swept_still_reads_back_by_name() -> None:
    written = attachments.materialise("sess-1", [SHOT])
    prompt = attachments.describe("look", written)
    attachments.cleanup("sess-1")
    assert attachments.split_prompt(prompt) == (
        "look",
        [{"name": "shot.png", "mime": "image/png", "size": 0}],
    )


def test_words_that_only_look_like_a_list_of_files_are_left_alone(tmp_path: Path) -> None:
    root = attachments.state_root()
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    for text in (
        "plain words",
        f"look\n\nAttached files:\n- secret.txt: {secret}",
        f"look\n\nAttached files:\n- a.png: {root}/../../secret.txt",
        "look\n\nAttached files:\n- a.png: relative/a.png",
        f"look\n\nAttached files:\n- a b.png: {root}/s/m/a.png",
        "look\n\nAttached files:\n",
    ):
        assert attachments.split_prompt(text) == (text, [])


# ------------------------------------------------------------- the sweep


def test_the_sweep_deletes_week_old_files_and_the_folders_they_leave_empty() -> None:
    old = planted("sess-1/aaaaaaaaaaaa/old.png", 10, days=8)
    legacy = planted("sess-2/flat.png", 20, days=9)
    recent = planted("sess-1/bbbbbbbbbbbb/new.png", 30, days=6)
    for folder in (old.parent, old.parent.parent, legacy.parent, recent.parent):
        aged(folder, days=8)

    swept = attachments.sweep()

    assert swept == attachments.Swept(files=2, bytes=30)
    assert not old.exists() and not legacy.exists()
    assert not old.parent.exists(), "an emptied message folder goes"
    assert not legacy.parent.exists(), "and so does a session folder left empty"
    assert recent.exists()
    assert attachments.state_root().is_dir()


def test_the_sweep_leaves_a_folder_a_send_may_be_filling() -> None:
    fresh = attachments.state_root() / "sess-1" / "cccccccccccc"
    fresh.mkdir(parents=True)
    attachments.sweep()
    assert fresh.is_dir()
    attachments.sweep(now=time.time() + 2 * attachments.FRESH)
    assert not fresh.exists()


def test_the_size_cap_deletes_the_oldest_first_until_the_root_fits() -> None:
    oldest = planted("sess-1/aaaaaaaaaaaa/1.png", 100, days=3)
    middle = planted("sess-1/bbbbbbbbbbbb/2.png", 100, days=2)
    newest = planted("sess-2/cccccccccccc/3.png", 100, days=1)

    swept = attachments.sweep(cap=250)

    assert swept == attachments.Swept(files=1, bytes=100)
    assert not oldest.exists()
    assert middle.exists() and newest.exists()
    assert attachments.sweep(cap=250) == attachments.Swept()


def test_the_sweep_never_follows_a_link_out_of_the_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.png"
    keep.write_bytes(b"mine")
    aged(keep, days=30)
    aged(outside, days=30)
    root = attachments.state_root()
    root.mkdir(parents=True)
    folder_link = root / "sess-1"
    folder_link.symlink_to(outside, target_is_directory=True)
    file_link = root / "sess-2" / "dddddddddddd" / "keep.png"
    file_link.parent.mkdir(parents=True)
    file_link.symlink_to(keep)
    aged(folder_link, days=30)
    aged(file_link, days=30)

    attachments.sweep()

    assert keep.read_bytes() == b"mine", "nothing outside the root is touched"
    assert not folder_link.is_symlink() and not file_link.is_symlink()
    assert outside.is_dir()


def test_a_state_folder_reached_through_a_link_is_used_where_it_really_is(
    tmp_path: Path,
) -> None:
    """A home on another volume: the state folder is resolved once and works."""
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    state_dir().parent.mkdir(parents=True, exist_ok=True)
    state_dir().symlink_to(elsewhere, target_is_directory=True)

    assert attachments.state_root() == elsewhere / "attachments"
    [own] = attachments.materialise("sess-1", [SHOT])
    assert Path(own.path).parent.parent.parent == elsewhere / "attachments"
    assert attachments.is_session_file("sess-1", own.path)
    aged(Path(own.path), days=8)
    assert attachments.sweep().files == 1
    attachments.cleanup("sess-1")
    assert not (elsewhere / "attachments" / "sess-1").exists()


def test_an_attachments_folder_that_is_a_link_is_refused_and_never_gone_through(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    (outside / "sess-1" / "0a1b2c3d4e5f").mkdir(parents=True)
    keep = outside / "sess-1" / "0a1b2c3d4e5f" / "keep.png"
    keep.write_bytes(b"mine")
    aged(keep, days=30)
    linked = state_dir() / "attachments"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RcError) as caught:
        attachments.materialise("sess-1", [SHOT])
    assert caught.value.message == attachments.LINKED_ROOT
    assert attachments.sweep() == attachments.Swept()
    attachments.cleanup("sess-1")
    attachments.discard([attachments.Attachment("keep.png", "image/png", 4, str(keep))])
    attachments.rekey("sess-1", "sess-9")
    assert keep.read_bytes() == b"mine", "nothing is written, swept or deleted through it"
    root = attachments.state_root()
    assert not attachments.is_session_file(
        "sess-1", str(root / "sess-1" / "0a1b2c3d4e5f" / "keep.png")
    )
    listed = f"look\n\nAttached files:\n- keep.png: {root}/sess-1/0a1b2c3d4e5f/keep.png"
    assert attachments.split_prompt(listed) == (listed, [])


def test_a_file_that_vanishes_during_the_sweep_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = planted("sess-1/aaaaaaaaaaaa/old.png", 10, days=8)
    scan = attachments._scan

    def racing(top: Path) -> Any:
        tree = scan(top)
        real.unlink()
        return tree

    monkeypatch.setattr(attachments, "_scan", racing)
    assert attachments.sweep() == attachments.Swept()


def test_the_sweep_logs_counts_and_bytes_and_never_a_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    planted("sess-1/aaaaaaaaaaaa/holiday-photo.png", 10, days=8)
    with caplog.at_level("INFO", logger="rc_client.attachments"):
        attachments.sweep()
    [record] = caplog.records
    assert record.__dict__["context"] == {"files": 1, "bytes": 10}
    assert "holiday" not in record.getMessage()


def test_nothing_to_sweep_is_not_an_error() -> None:
    assert attachments.sweep() == attachments.Swept()


# ----------------------------------------------------------- what deletes


def test_deleting_a_session_deletes_every_message_folder_it_had() -> None:
    attachments.materialise("sess-1", [SHOT])
    attachments.materialise("sess-1", [NOTES])
    planted("sess-1/flat.png", 1, days=0)
    other = attachments.materialise("sess-2", [SHOT])

    attachments.cleanup("sess-1")

    assert not (attachments.state_root() / "sess-1").exists()
    assert Path(other[0].path).exists()


def test_discarding_removes_only_that_message_s_folder() -> None:
    kept = attachments.materialise("sess-1", [SHOT])
    dropped = attachments.materialise("sess-1", [SHOT, NOTES])
    attachments.discard(dropped)
    assert message_folders("sess-1") == [Path(kept[0].path).parent]


def test_discarding_never_reaches_outside_the_root(tmp_path: Path) -> None:
    outside = tmp_path / "a" / "b"
    outside.mkdir(parents=True)
    keep = outside / "keep.png"
    keep.write_bytes(b"mine")
    attachments.discard([attachments.Attachment("keep.png", "image/png", 4, str(keep))])
    assert keep.exists()


async def test_the_hub_deletes_a_session_s_files_with_the_session(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    add_session(hub)
    attachments.materialise("sess-1", [SHOT])
    await hub.delete({"session_id": "sess-1"})
    assert not (attachments.state_root() / "sess-1").exists()
    registry.close()


async def test_a_message_held_for_a_driven_session_writes_nothing_until_it_goes(
    tmp_path: Path,
) -> None:
    """Its bytes wait in the queue, so taking it out leaves nothing behind."""
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    runner._busy = True
    await hub.send({"id": "req-1", "session_id": "sess-1", "text": "look", "attachments": [SHOT]})
    assert [item["id"] for item in entry.queue] == ["req-1"]
    assert message_folders("sess-1") == []
    await hub.queue_remove({"session_id": "sess-1", "queued_id": "req-1"})
    assert message_folders("sess-1") == []
    registry.close()


# --------------------------------------------------- a Claude session sent to


class StubSDK:
    def __init__(self, fails: bool = False) -> None:
        self.prompts: list[str] = []
        self.fails = fails

    async def query(self, prompt: str) -> None:
        if self.fails:
            raise ConnectionResetError("the CLI went away")
        self.prompts.append(prompt)

    async def disconnect(self) -> None:
        return None


def claude_runner(tmp_path: Path, frames: list[dict[str, Any]]) -> ClaudeRunner:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="sess-1", device_id="d", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    return ClaudeRunner(SessionChannel(registry, session, publish), binary=None, cwd=str(tmp_path))


def bubbles(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]


async def test_a_claude_prompt_names_the_files_and_the_bubble_only_their_names(
    tmp_path: Path,
) -> None:
    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    sdk = StubSDK()
    runner._client = sdk  # type: ignore[assignment]
    await runner.send("look", [SHOT])

    [folder] = message_folders("sess-1")
    assert sdk.prompts == [f"look\n\nAttached files:\n- shot.png: {folder / 'shot.png'}"]
    [bubble] = bubbles(frames)
    assert bubble["text"] == "look"
    assert bubble["attachments"] == [{"name": "shot.png", "mime": "image/png", "size": 5}]
    assert str(attachments.state_root()) not in json.dumps(frames)
    runner.channel.registry.close()


async def test_a_claude_prompt_that_never_reaches_the_cli_takes_its_files(
    tmp_path: Path,
) -> None:
    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    runner._client = StubSDK(fails=True)  # type: ignore[assignment]
    with pytest.raises(RcError) as caught:
        await runner.send("look", [SHOT])
    assert caught.value.code == "internal"
    assert message_folders("sess-1") == []
    runner.channel.registry.close()


# --------------------------------------------- reading them needs no approval


def test_a_file_written_for_the_session_is_its_own_to_read(tmp_path: Path) -> None:
    [own] = attachments.materialise("sess-1", [SHOT])
    [other] = attachments.materialise("sess-2", [SHOT])
    folder = Path(own.path).parent
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"x")

    assert attachments.is_session_file("sess-1", own.path)
    assert not attachments.is_session_file("sess-2", own.path), "only this session's own"
    assert not attachments.is_session_file("sess-1", other.path)
    assert not attachments.is_session_file("sess-1", str(folder)), "a folder is not a file"
    assert not attachments.is_session_file("sess-1", str(outside))
    assert not attachments.is_session_file("sess-1", f"{folder}/../../sess-2/x/shot.png")
    assert not attachments.is_session_file("sess-1", "sess-1/shot.png"), "never a relative path"
    assert not attachments.is_session_file("sess-1", None)
    assert not attachments.is_session_file("sess-1", str(folder / "missing.png"))


def test_no_link_on_the_way_makes_a_file_the_session_s_own(tmp_path: Path) -> None:
    [own] = attachments.materialise("sess-1", [SHOT])
    folder = Path(own.path).parent
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keys.png").write_bytes(b"secret")
    (folder / "linked.png").symlink_to(outside / "keys.png")
    (folder.parent / "aaaaaaaaaaaa").symlink_to(outside, target_is_directory=True)

    assert not attachments.is_session_file("sess-1", str(folder / "linked.png"))
    assert not attachments.is_session_file("sess-1", str(folder.parent / "aaaaaaaaaaaa/keys.png"))
    assert attachments.is_session_file("sess-1", own.path)


async def test_claude_reads_the_session_s_own_file_without_asking(tmp_path: Path) -> None:
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    [own] = attachments.materialise("sess-1", [SHOT])
    result = await runner._can_use_tool(
        "Read", {"file_path": own.path}, ToolPermissionContext(suggestions=[])
    )
    assert isinstance(result, PermissionResultAllow)
    assert [frame for frame in frames if frame.get("type") == "session.event"] == []
    runner.channel.registry.close()


async def test_claude_still_asks_before_reading_anything_else(tmp_path: Path) -> None:
    from claude_agent_sdk import ToolPermissionContext

    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    [other] = attachments.materialise("sess-2", [SHOT])
    asked = asyncio.create_task(
        runner._can_use_tool(
            "Read", {"file_path": other.path}, ToolPermissionContext(suggestions=[])
        )
    )
    while not runner._pending:
        await asyncio.sleep(0.01)
    await runner.approve(next(iter(runner._pending)), "deny", None)
    assert (await asked).behavior == "deny"
    approvals = [
        frame["event"]["status"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "approval"
    ]
    assert approvals == ["pending", "resolved"]
    await runner.channel.close()
    runner.channel.registry.close()


def test_only_the_path_as_written_and_a_file_with_one_name_count(tmp_path: Path) -> None:
    [own] = attachments.materialise("sess-1", [SHOT])
    folder = Path(own.path).parent
    assert not attachments.is_session_file("sess-1", f"{folder}//shot.png")
    assert not attachments.is_session_file("sess-1", f"{folder}/./shot.png")
    assert not attachments.is_session_file("sess-1", f"{own.path}\x00.txt")

    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    os.link(secret, folder / "hard.txt")
    assert not attachments.is_session_file("sess-1", str(folder / "hard.txt"))


def test_a_nul_byte_in_a_listed_path_is_read_as_a_missing_file() -> None:
    root = attachments.state_root()
    text = f"look\n\nAttached files:\n- a.png: {root}/s/0a1b2c3d4e5f/a\x00.png"
    assert attachments.split_prompt(text) == (
        "look",
        [{"name": "a.png", "mime": "image/png", "size": 0}],
    )


async def test_an_agent_title_or_error_never_carries_an_attachment_path(tmp_path: Path) -> None:
    from rc_client.sessions import titles

    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    [own] = attachments.materialise("sess-1", [SHOT])
    assert attachments.without_paths(f"Reading {own.path} now") == "Reading shot.png now"

    await titles.from_agent(runner.channel, f"Digits in {own.path}")
    await runner.channel.error(f"cannot open {own.path}: denied")
    assert runner.channel.session.title == "Digits in shot.png"
    assert str(attachments.state_root()) not in json.dumps(frames)
    runner.channel.registry.close()


async def test_claude_still_asks_before_writing_over_the_session_s_own_file(
    tmp_path: Path,
) -> None:
    from claude_agent_sdk import ToolPermissionContext

    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    [own] = attachments.materialise("sess-1", [SHOT])
    asked = asyncio.create_task(
        runner._can_use_tool(
            "Write", {"file_path": own.path, "content": "x"}, ToolPermissionContext(suggestions=[])
        )
    )
    while not runner._pending:
        await asyncio.sleep(0.01)
    await runner.approve(next(iter(runner._pending)), "deny", None)
    assert (await asked).behavior == "deny"
    await runner.channel.close()
    runner.channel.registry.close()


async def test_letting_go_of_a_terminal_session_marks_the_mirror_at_its_end(
    tmp_path: Path,
) -> None:
    """A39 and a daemon stop alike: what the runner published is not read again."""
    transcript = tmp_path / "terminal-1.jsonl"
    transcript.write_text('{"type": "user"}\n', encoding="utf-8")
    for close in ("archive", "daemon"):
        hub, _, registry = build_hub(tmp_path / close)
        entry = add_session(hub)
        entry.session.origin = "terminal"
        entry.transcript = str(transcript)
        if close == "archive":
            await hub.archive({"session_id": "sess-1", "archived": True})
        else:
            await hub.close()
        assert registry.get_kv("mirror-offset:sess-1") == str(transcript.stat().st_size), close
        registry.close()


@pytest.mark.parametrize("first", ["drain", "send"])
async def test_a_message_sent_while_the_queue_drains_waits_its_turn(
    tmp_path: Path, first: str
) -> None:
    """The held message is the runner's before anything sent meanwhile (A43).

    The drain task may hold the lock already, writing the held message's files,
    or not have started yet when the send takes the lock.
    """
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    runner = ClaudeRunner(entry.channel, binary=None, cwd=str(tmp_path), session_id="sess-1")
    sdk = StubSDK()
    runner._client = sdk  # type: ignore[assignment]
    entry.runner = runner
    runner._turn_done.clear()
    await hub.send(
        {"id": "req-x", "session_id": "sess-1", "text": "x", "attachments": [SHOT], "mode": "queue"}
    )
    runner._turn_done.set()

    hub.schedule_drain(entry)
    if first == "drain":
        await asyncio.sleep(0)
        assert entry.lock.locked()
    await hub.send({"id": "req-y", "session_id": "sess-1", "text": "y"})
    await drained(entry)

    assert [prompt.split("\n")[0] for prompt in sdk.prompts] == ["x"]
    assert [item["id"] for item in entry.queue] == ["req-y"]
    await entry.channel.close()
    registry.close()
