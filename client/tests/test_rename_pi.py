"""Amendment A55: renaming a pi session by whichever route its state allows.

Three holders, three routes: the `pi --mode rpc` child the device started (the fake
peer of `tests/fixtures/pi/fake_pi.py`), a pi in a terminal (a fake extension on the
real socket), and a session nothing holds, whose file is appended to. Every route goes
through `hub.set_options`, the frame an app sends. The TypeScript half of the terminal
route is not run here, since no real pi may be.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.agents.pi import frames, naming, session_file, slash
from rc_client.agents.pi import plugin as pi_plugin
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.agents.pi.adapter import PiRunner
from rc_client.agents.registry import DetectContext
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions import titles
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.rename_helpers import summaries, title_events
from tests.test_pi_extension import FIXTURES, Bench, FakeExtension, agent_info, connect, settle
from tests.test_pi_extension import SESSION as TERMINAL
from tests.test_pi_extension import bench as bench
from tests.test_pi_runner import SESSION as DRIVEN
from tests.test_pi_runner import Peer, Recorder, shim, wait_for

LEDGER = "Ledger migration"
EARLIER = "Typecheck the web app"
OLD_EXTENSION = f"unknown command {frames.SET_NAME}"
FIRST = "a1b2c3d4"
SECOND = "b2c3d4e5"


# ------------------------------------------------------------- an RPC child


async def driven(
    tmp_path: Path, mode: str = "turn", pi_name: str | None = None
) -> tuple[SessionHub, Recorder, Peer, SessionEntry]:
    """A session this device started, on a `pi --mode rpc` child the fake plays."""
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    hub = SessionHub(registry, recorder, "dev-1", agent_info)
    session = Session(
        session_id=DRIVEN,
        device_id="dev-1",
        agent="pi",
        cwd=str(tmp_path),
        origin="remote",
        control="remote",
    )
    entry = hub.register_mirrored(session)
    binary, peer = shim(tmp_path, mode)
    if pi_name is not None:
        (peer.directory / "name").write_text(pi_name, encoding="utf-8")
    entry.runner = PiRunner(
        entry.channel,
        binary=binary,
        cwd=str(tmp_path),
        session_id=DRIVEN,
        model="anthropic/claude-sonnet-4-5",
        permission_mode="on-request",
    )
    await entry.runner.start()
    return hub, recorder, peer, entry


async def test_a_child_the_device_started_is_asked_to_set_the_name(tmp_path: Path) -> None:
    hub, recorder, peer, entry = await driven(tmp_path)
    entry.session.title = EARLIER
    entry.session.updated_at = 1_000
    recorder.frames.clear()

    result = await hub.set_options({"session_id": DRIVEN, "title": "  Ledger\tmigration\n"})
    await wait_for(lambda: titles.pinned(hub.registry, DRIVEN))

    assert [command["name"] for command in peer.of("set_session_name")] == [LEDGER]
    assert result["session"]["title"] == LEDGER
    assert result["session"]["updated_at"] == 1_000
    # pi's own `session_info_changed` comes back too, and changes nothing more.
    assert title_events(recorder.frames) == [LEDGER]
    assert {summary["updated_at"] for summary in summaries(recorder.frames)} == {1_000}
    await entry.runner.close()  # type: ignore[union-attr]


async def test_a_name_pi_refuses_is_the_agents_refusal(tmp_path: Path) -> None:
    hub, recorder, _, entry = await driven(tmp_path, mode="refuse-name")
    entry.session.title = EARLIER
    recorder.frames.clear()

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": DRIVEN, "title": LEDGER})

    assert (caught.value.code, caught.value.message) == (
        "bad_request",
        "pi does not accept this name",
    )
    assert entry.session.title == EARLIER
    assert not titles.pinned(hub.registry, DRIVEN)
    assert title_events(recorder.frames) == []
    await entry.runner.close()  # type: ignore[union-attr]


async def test_a_child_that_has_exited_is_a_session_nothing_holds(tmp_path: Path) -> None:
    hub, _, peer, entry = await driven(tmp_path)
    runner = entry.runner
    assert isinstance(runner, PiRunner) and runner.alive
    process = runner._process
    assert process is not None and process._process is not None
    process._process.kill()
    await wait_for(lambda: not runner.alive)
    path = write_file(session_id=DRIVEN)

    await hub.set_options({"session_id": DRIVEN, "title": LEDGER})

    assert peer.of("set_session_name") == []
    assert entries_of(path)[-1]["name"] == LEDGER
    await runner.close()


async def test_a_name_pi_already_holds_is_the_users_when_the_child_starts(
    tmp_path: Path,
) -> None:
    """A session renamed in a terminal while the device was not attached."""
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    hub = SessionHub(registry, recorder, "dev-1", agent_info)
    session = Session(session_id=DRIVEN, device_id="dev-1", agent="pi", cwd=str(tmp_path))
    entry = hub.register_mirrored(session)
    await titles.from_user(entry.channel, "Chosen on the phone")
    binary, peer = shim(tmp_path, "turn")
    (peer.directory / "name").write_text("Renamed in pi", encoding="utf-8")
    runner = PiRunner(entry.channel, binary=binary, cwd=str(tmp_path), session_id=DRIVEN)

    await runner.start()

    assert entry.session.title == "Renamed in pi"
    assert titles.pinned(hub.registry, DRIVEN)
    await runner.close()


async def test_a_name_pi_reports_without_being_asked_is_the_users(tmp_path: Path) -> None:
    _, recorder, _, entry = await driven(tmp_path)
    runner = entry.runner
    assert isinstance(runner, PiRunner)
    await titles.from_user(entry.channel, "Chosen on the phone")
    entry.session.updated_at = 1_000
    recorder.frames.clear()

    await runner._stream.event({"type": "session_info_changed", "name": "Renamed in pi"})

    assert entry.session.title == "Renamed in pi"
    assert entry.session.updated_at == 1_000
    assert title_events(recorder.frames) == ["Renamed in pi"]
    await runner.close()


# ------------------------------------------------------------ a pi in a terminal


async def attached(bench: Bench) -> tuple[FakeExtension, SessionEntry]:
    extension = await connect(bench)
    await extension.hello()
    await settle()
    entry = bench.entry()
    entry.session.title = EARLIER
    entry.session.updated_at = 1_000
    bench.frames.clear()
    return extension, entry


async def renaming(bench: Bench, title: str = LEDGER) -> asyncio.Task[dict[str, Any]]:
    return asyncio.create_task(bench.hub.set_options({"session_id": TERMINAL, "title": title}))


async def test_a_pi_in_a_terminal_is_asked_through_its_extension(bench: Bench) -> None:
    extension, entry = await attached(bench)

    asking = await renaming(bench, "  Ledger\tmigration\n")
    command = await extension.serve_command({"name": LEDGER})
    result = await asking
    # pi tells the extension, and the extension forwards it, whoever asked.
    await extension.event({"type": "session_info_changed", "name": LEDGER})
    await settle()

    assert (command["command"], command["name"]) == (frames.SET_NAME, LEDGER)
    assert result["session"]["title"] == LEDGER
    assert result["session"]["updated_at"] == 1_000
    assert titles.pinned(bench.hub.registry, TERMINAL)
    assert title_events(bench.frames) == [LEDGER]
    assert entry.session.updated_at == 1_000
    extension.close()


async def test_the_name_the_extension_answers_is_the_one_that_is_kept(bench: Bench) -> None:
    extension, entry = await attached(bench)

    asking = await renaming(bench)
    await extension.serve_command({"name": f"{LEDGER} (2)"})
    result = await asking

    assert result["session"]["title"] == f"{LEDGER} (2)"
    assert entry.session.title == f"{LEDGER} (2)"
    extension.close()


async def test_an_answer_with_no_name_in_it_keeps_the_name_asked_for(bench: Bench) -> None:
    extension, _ = await attached(bench)

    asking = await renaming(bench)
    await extension.serve_command()
    result = await asking

    assert result["session"]["title"] == LEDGER
    extension.close()


async def refuse_command(extension: FakeExtension, error: str) -> dict[str, Any]:
    frame = await extension.receive()
    await extension.send(
        {"type": frames.REPLY, "id": frame["id"], "ok": False, "data": None, "error": error}
    )
    return frame


async def test_a_pi_started_before_the_extension_was_updated_has_to_be_restarted(
    bench: Bench,
) -> None:
    extension, entry = await attached(bench)

    asking = await renaming(bench)
    await refuse_command(extension, OLD_EXTENSION)

    with pytest.raises(RcError) as caught:
        await asking
    assert (caught.value.code, caught.value.message) == ("unsupported", "restart pi to rename it")
    assert entry.session.title == EARLIER
    assert not titles.pinned(bench.hub.registry, TERMINAL)
    assert title_events(bench.frames) == []
    extension.close()


async def test_any_other_refusal_from_the_extension_is_said_in_our_words(bench: Bench) -> None:
    extension, entry = await attached(bench)

    asking = await renaming(bench)
    await refuse_command(extension, "TypeError: not a function (/home/me/.pi/agent/x.ts)")

    with pytest.raises(RcError) as caught:
        await asking
    assert (caught.value.code, caught.value.message) == (
        "bad_request",
        "pi does not accept this name",
    )
    assert entry.session.title == EARLIER
    extension.close()


async def test_a_pi_that_goes_away_in_the_middle_of_it_cannot_be_renamed(bench: Bench) -> None:
    extension, entry = await attached(bench)

    asking = await renaming(bench)
    await extension.receive()
    extension.close()

    with pytest.raises(RcError) as caught:
        await asking
    assert caught.value.code == "agent_unavailable"
    assert entry.session.title == EARLIER
    assert not titles.pinned(bench.hub.registry, TERMINAL)


def test_the_name_goes_out_as_the_commands_own_field() -> None:
    assert frames.SET_NAME == "set_name"
    assert naming.UNKNOWN_COMMAND == "unknown command set_name"


async def test_a_name_in_the_terminal_replaces_a_name_an_app_gave(bench: Bench) -> None:
    extension, entry = await attached(bench)
    asking = await renaming(bench)
    await extension.serve_command({"name": LEDGER})
    await asking
    bench.frames.clear()

    await extension.event({"type": "session_info_changed", "name": "Renamed in pi"})
    await settle()

    assert entry.session.title == "Renamed in pi"
    assert entry.session.updated_at == 1_000
    assert title_events(bench.frames) == ["Renamed in pi"]
    extension.close()


@pytest.mark.parametrize("cleared", [None, "", "   ", 7])
async def test_a_name_that_was_cleared_names_nothing(bench: Bench, cleared: object) -> None:
    extension, entry = await attached(bench)
    await titles.from_user(entry.channel, LEDGER)
    bench.frames.clear()

    await extension.event({"type": "session_info_changed", "name": cleared})
    await settle()

    assert entry.session.title == LEDGER
    assert title_events(bench.frames) == []
    extension.close()


async def test_the_name_a_pi_has_when_it_connects_is_the_users(bench: Bench) -> None:
    extension = await connect(bench)
    await extension.hello(name="Named in pi")
    await settle()

    entry = bench.entry()
    assert entry.session.title == "Named in pi"
    assert titles.pinned(bench.hub.registry, TERMINAL)
    extension.close()

    # And one that reconnects after somebody renamed it follows the name it now has.
    again = await connect(bench)
    await again.hello(name="Renamed since")
    await settle()
    assert entry.session.title == "Renamed since"
    again.close()


# ------------------------------------------------------- a session nothing holds


def lone_session(hub: SessionHub, session_id: str = TERMINAL) -> SessionEntry:
    return hub.register_mirrored(
        Session(session_id=session_id, device_id="dev-1", agent="pi", cwd="/repo", control="none")
    )


@pytest.fixture
async def stored(tmp_path: Path) -> Any:
    registry = Registry(tmp_path / "state.sqlite3")
    recorder = Recorder()
    hub = SessionHub(registry, recorder, "dev-1", agent_info)
    entry = lone_session(hub)
    entry.session.title = EARLIER
    entry.session.updated_at = 1_000
    try:
        yield hub, recorder, entry
    finally:
        registry.close()


def message(entry_id: str, parent: str | None, role: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent,
        "timestamp": "2026-10-27T10:00:01.000Z",
        "message": {"role": role, "content": "hello", "timestamp": 1_789_000_000_000},
    }


def write_file(
    *,
    session_id: str = TERMINAL,
    version: object = 3,
    entries: list[dict[str, Any]] | None = None,
    header_id: str | None = None,
    tail: str = "\n",
    directory: str = "--repo--",
    stamp: str = "2026-10-27T10-00-00-000Z",
) -> Path:
    """A session file as pi lays one out under its sessions directory."""
    root = pi_runtime.sessions_dir() / directory
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stamp}_{session_id}.jsonl"
    header = {
        "type": "session",
        "version": version,
        "id": header_id or session_id,
        "timestamp": "2026-10-27T10:00:00.000Z",
        "cwd": "/repo",
    }
    rows = [header, *(entries if entries is not None else [message(FIRST, None, "user")])]
    path.write_text("\n".join(json.dumps(row) for row in rows) + tail, encoding="utf-8")
    return path


def entries_of(path: Path) -> list[dict[str, Any]]:
    """Every line that parses, which is how pi reads a file."""
    found: list[dict[str, Any]] = []
    for line in path.read_bytes().split(b"\n"):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            found.append(row)
    return found


async def test_a_session_nothing_holds_gets_a_session_info_entry_at_the_end(stored: Any) -> None:
    hub, recorder, entry = stored
    path = write_file(entries=[message(FIRST, None, "user"), message(SECOND, FIRST, "assistant")])
    before = path.read_bytes()
    recorder.frames.clear()

    result = await hub.set_options({"session_id": TERMINAL, "title": "  Ledger\tmigration\n"})

    written = path.read_bytes()
    assert written.startswith(before)
    new = entries_of(path)[-1]
    assert list(new) == ["type", "id", "parentId", "timestamp", "name"]
    assert (new["type"], new["parentId"], new["name"]) == ("session_info", SECOND, LEDGER)
    assert len(new["id"]) == 8 and new["id"] not in {FIRST, SECOND}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", new["timestamp"])
    assert written[len(before) :].count(b"\n") == 1
    assert result["session"]["title"] == LEDGER
    assert result["session"]["updated_at"] == 1_000
    assert titles.pinned(hub.registry, TERMINAL)
    assert title_events(recorder.frames) == [LEDGER]
    assert entry.session.updated_at == 1_000


async def test_the_entry_is_written_as_pi_writes_one(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file()

    await hub.set_options({"session_id": TERMINAL, "title": "日本語 \U0001f642"})

    last = path.read_bytes().split(b"\n")[-2]
    # Compact JSON with the name as it is, which is what `JSON.stringify` writes.
    assert last.startswith(b'{"type":"session_info","id":"')
    assert "日本語 \U0001f642".encode() in last


async def test_a_session_with_only_a_header_hangs_the_entry_from_nothing(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file(entries=[])

    await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert entries_of(path)[-1]["parentId"] is None


async def test_a_crash_in_the_middle_of_a_line_is_repaired_before_the_entry(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file(
        entries=[message(FIRST, None, "user"), message(SECOND, FIRST, "assistant")],
        tail='\n{"type":"message","id":"c3d4e5f6","parentId":"b2c3',
    )

    await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    raw = path.read_bytes()
    assert b'"parentId":"b2c3\n{"type":"session_info"' in raw
    assert raw.endswith(b"\n")
    new = entries_of(path)[-1]
    # The stump is a line pi skips, so the last entry that counts is the one before it.
    assert (new["name"], new["parentId"]) == (LEDGER, SECOND)


async def test_a_last_entry_with_no_newline_still_counts_as_the_last_entry(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file(
        entries=[message(FIRST, None, "user"), message(SECOND, FIRST, "assistant")], tail=""
    )

    await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    rows = entries_of(path)
    assert [row.get("id") for row in rows[1:3]] == [FIRST, SECOND]
    assert rows[-1]["parentId"] == SECOND
    assert path.read_bytes().count(b"\n") == 4


async def test_an_entry_id_is_never_one_the_file_already_has(
    stored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, _ = stored
    path = write_file(entries=[message(FIRST, None, "user")])
    drawn = iter([FIRST, "0f1e2d3c"])
    monkeypatch.setattr(
        "rc_client.agents.pi.session_file.uuid.uuid4",
        lambda: SimpleNamespace(hex=next(drawn) + "0" * 24),
    )

    await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert entries_of(path)[-1]["id"] == "0f1e2d3c"


async def test_a_file_in_an_older_format_is_left_alone(stored: Any) -> None:
    hub, _, entry = stored
    path = write_file(version=2)
    before = path.read_bytes()

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert (caught.value.code, caught.value.message) == ("unsupported", session_file.OLD_FORMAT)
    assert path.read_bytes() == before
    assert entry.session.title == EARLIER
    assert not titles.pinned(hub.registry, TERMINAL)


async def test_a_file_without_a_version_is_an_older_one(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file(version=None)
    before = path.read_bytes()

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert caught.value.message == session_file.OLD_FORMAT
    assert path.read_bytes() == before


async def test_a_session_a_terminal_holds_out_of_reach_is_left_to_that_terminal(
    stored: Any,
) -> None:
    """A running pi would take an entry appended behind its back for a side branch."""
    hub, _, entry = stored
    entry.session.control = "terminal"
    path = write_file()
    before = path.read_bytes()

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert (caught.value.code, caught.value.message) == (
        "unsupported",
        "rename it in the terminal",
    )
    assert path.read_bytes() == before
    assert not titles.pinned(hub.registry, TERMINAL)


async def test_no_file_is_made_for_a_session_that_has_none(stored: Any) -> None:
    """A session is written to disk with its first answer, so before that it has no file."""
    hub, _, entry = stored

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert (caught.value.code, caught.value.message) == ("unsupported", session_file.MISSING)
    assert not pi_runtime.sessions_dir().exists()
    assert entry.session.title == EARLIER
    assert not titles.pinned(hub.registry, TERMINAL)


async def test_a_file_whose_header_names_another_session_is_not_this_one(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file(header_id="someone-else")
    before = path.read_bytes()

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert caught.value.message == session_file.MISSING
    assert path.read_bytes() == before


async def test_a_file_that_is_not_a_session_is_not_written_to(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file()
    path.write_text('{"type":"message","id":"a1b2c3d4"}\n', encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert caught.value.code == "unsupported"
    assert path.read_bytes() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only file")
async def test_a_file_that_cannot_be_written_is_refused_without_its_path(stored: Any) -> None:
    hub, _, _ = stored
    path = write_file()
    path.chmod(0o444)
    try:
        with pytest.raises(RcError) as caught:
            await hub.set_options({"session_id": TERMINAL, "title": LEDGER})
    finally:
        path.chmod(0o644)

    assert (caught.value.code, caught.value.message) == ("unsupported", session_file.UNWRITABLE)
    assert str(path) not in caught.value.message


def test_a_session_id_that_could_be_a_pattern_or_a_path_finds_nothing() -> None:
    write_file()
    for hostile in ("*", "../x", "a/b", "a b", "[a-z]*", "", "-lead", "trail."):
        assert session_file.find(hostile) is None
    assert session_file.find(TERMINAL) is not None


def test_the_newest_of_two_files_that_claim_one_session_is_the_one_written(
    tmp_path: Path,
) -> None:
    older = write_file(directory="--repo--", stamp="2026-10-26T10-00-00-000Z")
    newer = write_file(directory="--other--", stamp="2026-10-27T10-00-00-000Z")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))

    assert session_file.find(TERMINAL) == newer


def test_appending_never_creates_the_file(tmp_path: Path) -> None:
    missing = tmp_path / "gone.jsonl"

    with pytest.raises(RcError) as caught:
        session_file.append_name(missing, LEDGER)

    assert caught.value.code == "unsupported"
    assert not missing.exists()


def test_a_file_that_vanishes_after_it_was_read_is_not_made_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_file()
    reading = session_file._leaf

    def read_then_lose(target: Path) -> session_file.Leaf:
        leaf = reading(target)
        target.unlink()
        return leaf

    monkeypatch.setattr(session_file, "_leaf", read_then_lose)

    with pytest.raises(RcError) as caught:
        session_file.append_name(path, LEDGER)

    assert (caught.value.code, caught.value.message) == ("unsupported", session_file.UNWRITABLE)
    assert not path.exists()


async def test_the_file_is_read_and_written_off_the_event_loop(
    stored: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, _ = stored
    write_file()
    threads: list[str] = []
    real = session_file.name_session

    def recording(session_id: str, name: str) -> None:
        threads.append(threading.current_thread().name)
        real(session_id, name)

    monkeypatch.setattr(session_file, "name_session", recording)

    await hub.set_options({"session_id": TERMINAL, "title": LEDGER})

    assert threads and threads != [threading.main_thread().name]


# ------------------------------------------------------------ what is not a command


def test_pis_own_name_command_is_never_one_of_a_sessions_commands() -> None:
    """`/name` is `session.set` (A55), and pi's list never carries its built-ins."""
    recorded = json.loads((FIXTURES / "commands.json").read_text(encoding="utf-8"))
    assert "name" not in {command.name for command in slash.from_agent(recorded["commands"])}
    assert "name" not in {command.name for command in slash.offline()}


async def test_a_session_does_not_run_name_as_a_command(tmp_path: Path) -> None:
    _, _, peer, entry = await driven(tmp_path)
    runner = entry.runner
    assert isinstance(runner, PiRunner)

    with pytest.raises(RcError) as caught:
        await runner.command("name", LEDGER, "b1")

    assert caught.value.code == "not_found"
    assert peer.of("prompt") == [] and peer.of("set_session_name") == []
    await runner.close()


async def test_detection_lists_rename_last_and_the_plugin_has_the_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a binary found, detection runs `pi --version` and `pi --list-models`, so none
    # may be found.
    monkeypatch.setattr(pi_runtime, "resolve_binary", lambda: None)

    info = await pi_plugin.detect(DetectContext())

    assert info.capabilities[-1] == "rename"
    assert callable(getattr(registry.plugin("pi"), "rename", None))
