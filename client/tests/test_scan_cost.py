"""What a terminal scan costs.

One look at who writes every rollout instead of one helper process per file, the
files that left the discovery window dropped, the shared Codex daemon kept on a
loop of its own whose listings neither wait a minute nor hold anything else back,
and one line per scan saying where its time went.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from rc_client import procscan
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.codex.daemon import service as service_module
from rc_client.agents.codex.daemon import threads
from rc_client.logging_setup import JsonFormatter
from rc_client.models import Session
from rc_client.procscan import Proc, Scan, Writers
from rc_client.registry import Registry
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions import scan_timing
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_codex_daemon_sessions import Harness, harness, indexed, thread_row

__all__ = ["harness"]

# ------------------------------------------------------------- the writer scan


def test_only_regular_files_open_for_writing_are_writers() -> None:
    report = "\n".join(
        [
            "p100",
            "fcwd",
            "a ",
            "tDIR",
            "n/repo",
            "f12",
            "aw",
            "tREG",
            "n/codex/sessions/rollout-a.jsonl",
            "f13",
            "ar",
            "tREG",
            "n/codex/sessions/rollout-b.jsonl",
            "f14",
            "au",
            "tFIFO",
            "npipe",
            "p200",
            "f3",
            "au",
            "tREG",
            "n/codex/sessions/rollout-c.jsonl",
        ]
    )
    assert procscan._parse_written(report) == {
        100: ["/codex/sessions/rollout-a.jsonl"],
        200: ["/codex/sessions/rollout-c.jsonl"],
    }


async def test_every_writer_comes_from_one_look_at_the_named_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    procs = [
        Proc(pid=10, ppid=1, start="s10", command="/usr/local/bin/codex"),
        Proc(pid=11, ppid=1, start="s11", command="node /usr/local/bin/codex resume"),
        Proc(pid=12, ppid=1, start="s12", command="/bin/zsh"),
        # The device's own children are never a terminal's.
        Proc(pid=13, ppid=os.getpid(), start="s13", command="/usr/local/bin/codex app-server"),
    ]
    asked: list[list[int]] = []

    async def scan() -> Scan:
        return Scan(procs=procs, complete=True)

    async def written(pids: list[int]) -> tuple[dict[int, list[str]], bool]:
        asked.append(pids)
        return {10: [str(rollout)], 11: [str(tmp_path / "other.jsonl")]}, True

    monkeypatch.setattr(procscan, "scan_processes", scan)
    monkeypatch.setattr(procscan, "_written_paths", written)
    writers = await procscan.file_writers(codex_rollouts.WRITERS)

    assert asked == [[10, 11]]
    assert writers.complete is True
    assert [proc.pid for proc in writers.of(str(rollout))] == [10]
    assert writers.of(str(tmp_path / "nobody.jsonl")) == ()


async def test_an_unfinished_process_table_is_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scan() -> Scan:
        return Scan(procs=[], complete=False)

    monkeypatch.setattr(procscan, "scan_processes", scan)
    assert await procscan.file_writers(codex_rollouts.WRITERS) == Writers({}, False)


# --------------------------------------------------------- the rollout mirror


def rollout_info(root: Path, thread_id: str) -> codex_rollouts.RolloutInfo:
    path = root / f"rollout-2026-11-16T10-00-00-{thread_id}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return codex_rollouts.RolloutInfo(
        thread_id=thread_id, path=str(path), cwd="/repo", size=3, mtime=1.0
    )


class WriterScans:
    """Stands in for `file_writers`, counting the passes it is asked for."""

    def __init__(self) -> None:
        self.calls = 0
        self.writing: dict[str, tuple[Proc, ...]] = {}

    async def __call__(self, names: frozenset[str]) -> Writers:
        self.calls += 1
        return Writers(dict(self.writing), True)


@pytest.fixture
def quiet_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Claude or Grok sessions anywhere, so a scan is about Codex alone."""
    from rc_client.agents.claude import transcripts as claude_transcripts
    from rc_client.agents.claude.holders import HolderScan
    from rc_client.agents.grok import sessions as grok_sessions

    async def no_holders(*args: Any, **kwargs: Any) -> HolderScan:
        return HolderScan(holders=[], complete=True)

    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: [])
    monkeypatch.setattr(grok_sessions, "discover", lambda *args: [])
    monkeypatch.setattr(mirror_module, "scan_holders", no_holders)


async def test_rollouts_that_left_the_window_stop_being_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_scan: None
) -> None:
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    mirror = MirrorService(hub)
    writers = WriterScans()
    monkeypatch.setattr(mirror_module, "file_writers", writers)
    window = [rollout_info(tmp_path, name) for name in ("t-old", "t-live", "t-new")]
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: list(window))
    tui = Proc(pid=70, ppid=1, start="s70", command="/usr/local/bin/codex")
    writers.writing = {os.path.realpath(window[1].path): (tui,)}
    try:
        await mirror.scan_once(settings=False)
        assert sorted(mirror._codex) == ["t-live", "t-new", "t-old"]
        # Every rollout's writers came from one pass, whatever the number of rollouts.
        assert writers.calls == 1
        assert hub.entry("t-live").session.control == "terminal"
        assert hub.entry("t-live").holder_pid == 70
        assert hub.entry("t-old").session.control == "none"

        # Two rollouts fall out of the window; only the one a terminal holds is still read.
        window[:] = [window[2]]
        await mirror.scan_once(settings=False)
        assert sorted(mirror._codex) == ["t-live", "t-new"]
        assert writers.calls == 2
        # The session itself stays: only the reading of its file stops.
        assert "t-old" in hub.entries
    finally:
        await hub.close()
        registry.close()


async def test_an_unfinished_writer_scan_changes_no_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quiet_scan: None
) -> None:
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    mirror = MirrorService(hub)
    info = rollout_info(tmp_path, "t-tui")
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [info])
    hub.register_mirrored(
        Session(
            session_id="t-tui",
            device_id="dev-1",
            agent="codex",
            cwd="/repo",
            state="readonly",
            origin="terminal",
            control="terminal",
        )
    )

    async def unfinished(names: frozenset[str]) -> Writers:
        return Writers({}, False)

    monkeypatch.setattr(mirror_module, "file_writers", unfinished)
    try:
        await mirror.scan_once(settings=False)
        assert hub.entry("t-tui").session.control == "terminal"
    finally:
        await hub.close()
        registry.close()


# ------------------------------------------------------------ the daemon tick


async def test_a_listing_that_times_out_is_left_alone_for_a_while(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "LIST_TIMEOUT", 0.2)
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    harness.daemon.silent.add("thread/list")
    await indexed(harness)

    def listings() -> int:
        return sum(1 for method, _ in harness.daemon.calls if method == "thread/list")

    def loaded_listings() -> int:
        return sum(1 for method, _ in harness.daemon.calls if method == "thread/loaded/list")

    assert listings() == 1
    before = loaded_listings()
    await harness.service.refresh()
    # The history waits out its back-off; what is loaded is still asked every time.
    assert listings() == 1
    assert loaded_listings() == before + 1

    harness.daemon.silent.discard("thread/list")
    harness.daemon.replies["thread/list"] = {"data": [thread_row()]}
    monkeypatch.setattr(harness.service, "_history_at", 0.0)
    await harness.service.refresh()
    assert listings() == 2
    assert harness.service._history_wait == 0.0


async def test_a_thread_started_during_a_slow_listing_is_taken_at_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing the device holds waits for the daemon to finish listing."""
    monkeypatch.setattr(service_module, "LIST_TIMEOUT", 5.0)
    harness.daemon.replies["thread/list"] = {"data": [thread_row("t-old")]}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    await indexed(harness)

    harness.daemon.silent.add("thread/list")
    refresh = asyncio.create_task(harness.service.refresh())
    await asyncio.sleep(0.1)
    fresh = thread_row("t-new", originator="codex-tui", source="cli")
    await harness.daemon.notify("thread/started", {"thread": fresh})

    async def adopted() -> None:
        while "t-new" not in harness.hub.entries:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(adopted(), timeout=2)
    assert not refresh.done()
    refresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh


async def test_a_listing_never_deletes_a_thread_learnt_after_it_began(harness: Harness) -> None:
    harness.daemon.replies["thread/list"] = {"data": [thread_row("t-old")]}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    await indexed(harness)
    fresh = thread_row("t-new", originator="codex-tui", source="cli")
    await harness.daemon.notify("thread/started", {"thread": fresh})

    async def adopted() -> None:
        while "t-new" not in harness.hub.entries:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(adopted(), timeout=2)
    learnt = harness.service._known["t-new"]
    old = threads.ThreadSummary.parse(thread_row("t-old"))
    assert old is not None
    # A complete page that does not name it, from a listing that began before it was known.
    await harness.service._forget_deleted([old], set(), complete=True, listed_at=learnt - 1)
    assert "t-new" in harness.hub.entries
    # The same page from a listing that began after it was known is proof it is gone.
    await harness.service._forget_deleted([old], set(), complete=True, listed_at=learnt + 1)
    assert "t-new" not in harness.hub.entries


# --------------------------------------------------------------- scan timing


def test_a_slow_scan_says_where_its_time_went(caplog: pytest.LogCaptureFixture) -> None:
    ticks = iter([0.0, 0.5, 2.5, 3.0])
    timer = scan_timing.ScanTimer(clock=lambda: next(ticks))
    timer.lap("claude_control")
    timer.lap("codex_control")
    timer.lap("titles")
    with caplog.at_level(logging.DEBUG, logger="rc_client.mirror"):
        timer.report(codex=3)
    record = caplog.records[-1]
    assert record.levelno == logging.INFO
    assert record.context == {  # type: ignore[attr-defined]
        "claude_control_ms": 500,
        "codex_control_ms": 2000,
        "titles_ms": 500,
        "total_ms": 3000,
        "codex": 3,
    }
    # As the log has it: Codex is an agent's name, and its parts are not hidden as secrets.
    line = json.loads(JsonFormatter().format(record))
    assert (line["codex_control_ms"], line["codex"]) == (2000, 3)


def test_a_secret_beside_an_agent_name_is_still_hidden() -> None:
    record = logging.LogRecord("rc_client.mirror", logging.INFO, __file__, 1, "x", None, None)
    record.context = {"codex_token": "t", "code": "123456", "codex_home": "/h"}
    line = json.loads(JsonFormatter().format(record))
    assert (line["codex_token"], line["code"], line["codex_home"]) == (
        "<redacted>",
        "<redacted>",
        "/h",
    )


def test_a_quick_scan_is_said_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    ticks = iter([0.0, 0.1])
    timer = scan_timing.ScanTimer(clock=lambda: next(ticks))
    timer.lap("claude_control")
    with caplog.at_level(logging.DEBUG, logger="rc_client.mirror"):
        timer.report()
    assert caplog.records[-1].levelno == logging.DEBUG
