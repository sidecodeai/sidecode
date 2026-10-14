"""Phase 3: the device makes the shared Codex daemon run, and keeps it current.

A TUI never starts the daemon itself, so a machine whose daemon is down has
every terminal Codex on takeover-only until something else starts it. These are
the rules for that something else: start it, supervise it, and restart it when
an upgrade has left it behind — without ever downloading anything, and without
interrupting whoever is in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex.daemon import control, setup
from rc_client.agents.codex.daemon import repair as repair_module
from rc_client.agents.codex.daemon.repair import DaemonRepair
from rc_client.agents.codex.models import MAX_CACHED_BUILDS, CatalogCache
from rc_client.service import codex as supervision

BINARY = "/home/u/.codex/packages/standalone/current/bin/codex"


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeControl:
    """The `codex app-server daemon` subcommands, recorded rather than run."""

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.restarts: list[str] = []
        self.reads: list[str] = []
        self.installs: list[str] = []
        self.start_ok = True
        self.start_detail = "started"
        self.restart_ok = True
        self.found: control.DaemonVersion | None = None

    async def start(self, binary: str) -> tuple[bool, str]:
        self.starts.append(binary)
        return self.start_ok, self.start_detail

    async def restart(self, binary: str) -> tuple[bool, str]:
        self.restarts.append(binary)
        return self.restart_ok, "restarted" if self.restart_ok else "no daemon is running"

    async def version(self, binary: str) -> control.DaemonVersion | None:
        self.reads.append(binary)
        return self.found

    def install(self, binary: str) -> Path:
        self.installs.append(binary)
        return Path(binary + ".plist")


class Recorder:
    """The logger, so "logged once" can be asserted rather than assumed."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, message: str, **fields: Any) -> None:
        self.warnings.append(message)

    def info(self, message: str, **fields: Any) -> None:
        self.infos.append(message)

    def debug(self, message: str, **fields: Any) -> None:
        pass


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch) -> FakeControl:
    """A machine with the standalone build installed and nothing else real."""
    fake = FakeControl()
    monkeypatch.delenv("RC_CODEX_DAEMON_SOCKET", raising=False)
    monkeypatch.setattr(setup, "standalone_binary", lambda: BINARY)
    monkeypatch.setattr(control, "start", fake.start)
    monkeypatch.setattr(control, "restart", fake.restart)
    monkeypatch.setattr(control, "version", fake.version)
    monkeypatch.setattr(supervision, "status", lambda: "loaded")
    monkeypatch.setattr(supervision, "install", fake.install)
    return fake


async def safe() -> bool:
    return True


async def occupied() -> bool:
    return False


# ------------------------------------------------------- starting the daemon


async def test_the_device_starts_the_daemon_because_nothing_else_will(lab: FakeControl) -> None:
    """Ruling R1: a bare TUI runs its app-server embedded and never creates the socket."""
    healer = DaemonRepair(Clock())
    assert await healer.ensure_running() is True
    assert lab.starts == [BINARY]


async def test_a_daemon_that_was_already_running_is_not_a_failure(lab: FakeControl) -> None:
    lab.start_detail = "alreadyRunning"
    assert await DaemonRepair(Clock()).ensure_running() is True


async def test_the_start_is_not_repeated_more_than_once_a_minute(lab: FakeControl) -> None:
    clock = Clock()
    healer = DaemonRepair(clock)
    await healer.ensure_running()
    await healer.ensure_running()
    assert len(lab.starts) == 1
    clock.advance(repair_module.START_INTERVAL + 1)
    await healer.ensure_running()
    assert len(lab.starts) == 2


async def test_three_failures_slow_the_retry_to_ten_minutes(lab: FakeControl) -> None:
    lab.start_ok = False
    lab.start_detail = "managed standalone Codex install not found"
    clock = Clock()
    healer = DaemonRepair(clock)
    for _ in range(repair_module.FAILURE_LIMIT):
        assert await healer.ensure_running() is False
        clock.advance(repair_module.START_INTERVAL + 1)
    assert len(lab.starts) == repair_module.FAILURE_LIMIT
    await healer.ensure_running()
    assert len(lab.starts) == repair_module.FAILURE_LIMIT
    clock.advance(repair_module.BACKOFF_INTERVAL)
    await healer.ensure_running()
    assert len(lab.starts) == repair_module.FAILURE_LIMIT + 1


async def test_a_failure_is_logged_once_not_once_a_minute(
    lab: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(repair_module, "log", recorder)
    lab.start_ok = False
    lab.start_detail = "managed standalone Codex install not found"
    clock = Clock()
    healer = DaemonRepair(clock)
    await healer.ensure_running()
    clock.advance(repair_module.START_INTERVAL + 1)
    await healer.ensure_running()
    assert len(recorder.warnings) == 1
    assert "managed standalone Codex install not found" in recorder.warnings[0]
    lab.start_detail = "permission denied"
    clock.advance(repair_module.START_INTERVAL + 1)
    await healer.ensure_running()
    assert len(recorder.warnings) == 2


async def test_the_device_supervises_a_machine_that_has_no_nudge(
    lab: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R1: a device enrolled before the daemon existed has nothing keeping it up."""
    monkeypatch.setattr(supervision, "status", lambda: "not installed")
    clock = Clock()
    healer = DaemonRepair(clock)
    await healer.ensure_running()
    assert lab.installs == [BINARY]
    clock.advance(repair_module.START_INTERVAL + 1)
    await healer.ensure_running()
    assert lab.installs == [BINARY]


async def test_supervision_the_machine_already_has_is_left_alone(lab: FakeControl) -> None:
    await DaemonRepair(Clock()).ensure_running()
    assert lab.installs == []


async def test_a_machine_without_codex_is_never_touched(
    lab: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R2: installing Codex stays in `rc-client codex setup`, where a person asked."""

    async def never_download() -> tuple[bool, str]:
        raise AssertionError("the running device must never download an installer")

    monkeypatch.setattr(setup, "standalone_binary", lambda: None)
    monkeypatch.setattr(setup, "install_codex", never_download)
    healer = DaemonRepair(Clock())
    assert await healer.ensure_running() is False
    assert await healer.check_drift(safe) is False
    assert (lab.starts, lab.reads, lab.installs) == ([], [], [])


async def test_a_socket_somebody_else_owns_is_never_started(
    lab: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`daemon start` only ever creates the socket under `CODEX_HOME`."""
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", "/tmp/somebody-elses.sock")
    healer = DaemonRepair(Clock())
    assert await healer.ensure_running() is False
    assert await healer.check_drift(safe) is False
    assert (lab.starts, lab.reads) == ([], [])


# --------------------------------------------------------------- the drift


def drifted() -> control.DaemonVersion:
    return control.DaemonVersion("running", app_server="0.153.0", managed="0.154.0", cli="0.154.0")


def current() -> control.DaemonVersion:
    return control.DaemonVersion("running", app_server="0.154.0", managed="0.154.0", cli="0.154.0")


async def test_a_daemon_left_behind_by_an_upgrade_is_restarted(lab: FakeControl) -> None:
    """Ruling R3: `daemon start` returns `alreadyRunning` for ever; only `restart` clears it."""
    lab.found = drifted()
    assert await DaemonRepair(Clock()).check_drift(safe) is True
    assert lab.restarts == [BINARY]


async def test_a_daemon_on_the_installed_build_is_left_running(lab: FakeControl) -> None:
    lab.found = current()
    assert await DaemonRepair(Clock()).check_drift(safe) is False
    assert lab.restarts == []


async def test_a_drift_waits_while_somebody_is_in_the_daemon(
    lab: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart drops every subscriber, and a drifted daemon still works perfectly."""
    recorder = Recorder()
    monkeypatch.setattr(repair_module, "log", recorder)
    lab.found = drifted()
    clock = Clock()
    healer = DaemonRepair(clock)
    assert await healer.check_drift(occupied) is False
    clock.advance(repair_module.DRIFT_INTERVAL)
    assert await healer.check_drift(occupied) is False
    assert lab.restarts == []
    assert len(recorder.infos) == 1
    clock.advance(repair_module.DRIFT_INTERVAL)
    assert await healer.check_drift(safe) is True
    assert lab.restarts == [BINARY]


async def test_the_drift_check_runs_every_fifteen_minutes_at_most(lab: FakeControl) -> None:
    lab.found = current()
    clock = Clock()
    healer = DaemonRepair(clock)
    await healer.check_drift(safe)
    await healer.check_drift(safe)
    assert len(lab.reads) == 1
    clock.advance(repair_module.DRIFT_INTERVAL)
    await healer.check_drift(safe)
    assert len(lab.reads) == 2


async def test_a_daemon_that_answers_nothing_is_not_restarted(lab: FakeControl) -> None:
    lab.found = None
    assert await DaemonRepair(Clock()).check_drift(safe) is False
    assert lab.restarts == []


async def test_a_refused_restart_is_reported_and_not_retried_at_once(
    lab: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(repair_module, "log", recorder)
    lab.found = drifted()
    lab.restart_ok = False
    clock = Clock()
    healer = DaemonRepair(clock)
    assert await healer.check_drift(safe) is False
    assert await healer.check_drift(safe) is False
    assert len(lab.restarts) == 1
    assert len(recorder.warnings) == 1


# ------------------------------------------------- the catalogue and the build


async def test_the_model_catalogue_follows_the_build_it_was_read_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R4: `current` is a symlink into a per-version release directory."""
    releases = tmp_path / "releases"
    old = releases / "0.153.0/bin/codex"
    new = releases / "0.154.0/bin/codex"
    for binary in (old, new):
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
    link = tmp_path / "current"
    link.symlink_to(old.parent.parent)
    asked: list[str] = []

    async def catalogue(binary: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        asked.append(binary)
        version = Path(binary).resolve().parent.parent.name
        return {"data": [{"id": f"gpt-{version}", "isDefault": True}]}

    monkeypatch.setattr("rc_client.agents.codex.models.one_shot", catalogue)
    cache = CatalogCache()
    through_link = str(link / "bin/codex")
    first = await cache.get(through_link)
    assert first.default_model == "gpt-0.153.0"
    assert (await cache.get(through_link)).default_model == "gpt-0.153.0"
    assert len(asked) == 1

    link.unlink()
    link.symlink_to(new.parent.parent)
    assert (await cache.get(through_link)).default_model == "gpt-0.154.0"
    assert len(asked) == 2


async def test_a_failed_catalogue_read_keeps_the_one_that_build_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    answers: list[Any] = [
        {"data": [{"id": "gpt-5.4-codex", "isDefault": True}]},
        RuntimeError("no daemon"),
    ]

    async def flaky(path: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    monkeypatch.setattr("rc_client.agents.codex.models.one_shot", flaky)
    # Nothing is fresh, so every call is a real read and the failure is the
    # second one: the catalogue this build had is what it keeps.
    monkeypatch.setattr("rc_client.agents.codex.models.CACHE_TTL", 0.0)
    cache = CatalogCache()
    assert (await cache.get(str(binary))).default_model == "gpt-5.4-codex"
    assert (await cache.get(str(binary))).default_model == "gpt-5.4-codex"
    assert answers == []


async def test_only_a_handful_of_builds_are_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device that runs for months meets a new release every few weeks."""
    asked: list[str] = []

    async def catalogue(binary: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        asked.append(binary)
        return {"data": [{"id": Path(binary).name, "isDefault": True}]}

    monkeypatch.setattr("rc_client.agents.codex.models.one_shot", catalogue)
    cache = CatalogCache()
    builds = []
    for index in range(MAX_CACHED_BUILDS + 2):
        binary = tmp_path / f"codex-{index}"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        builds.append(binary)
        assert (await cache.get(str(binary))).default_model == binary.name
    # The oldest build was dropped, the newest is still there.
    await cache.get(str(builds[0]))
    assert asked.count(str(builds[0])) == 2
    await cache.get(str(builds[-1]))
    assert asked.count(str(builds[-1])) == 1
