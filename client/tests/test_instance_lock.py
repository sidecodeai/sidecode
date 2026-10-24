"""One daemon per device home: the lock that keeps a second one from deleting the sockets."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import select
import signal
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from rc_client import daemon as daemon_module
from rc_client import instance_lock
from rc_client.agents.pi import paths as pi_paths
from rc_client.channel import paths as channel_paths
from rc_client.cli import EXIT_FAILURE, main
from rc_client.config import Config, save_config
from rc_client.daemon import Daemon
from rc_client.errors import RcError
from rc_client.instance_lock import InstanceLock, lock_path

# Nothing listens on the discard port, so a daemon that did start would reach no gateway.
CONFIG = Config(
    gateway_origin="http://127.0.0.1:9",
    device_id="dev-1",
    device_token="tok-1",
    name="test-device",
)

# Takes the lock the way a daemon does and then waits to be killed.
HOLDER = """
import sys, time
from pathlib import Path
from rc_client.instance_lock import InstanceLock

lock = InstanceLock(Path(sys.argv[1]))
assert lock.try_acquire()
print("held", flush=True)
time.sleep(120)
"""


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "daemon.lock"


@pytest.fixture
def quick_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up after a tenth of a second instead of twenty."""
    monkeypatch.setattr(instance_lock, "WAIT_SECONDS", 0.1)
    monkeypatch.setattr(instance_lock, "POLL_SECONDS", 0.01)


def is_free(path: Path) -> bool:
    """Whether nobody holds the lock at `path` right now."""
    probe = InstanceLock(path)
    free = probe.try_acquire()
    probe.release()
    return free


@contextlib.contextmanager
def a_holder_process(path: Path) -> Iterator[subprocess.Popen[bytes]]:
    """Another process that holds the lock at `path` until it is killed."""
    command = [sys.executable, "-c", HOLDER, str(path)]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        try:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 30)
            assert ready
            assert process.stdout.readline() == b"held\n"
            yield process
        finally:
            process.kill()


@contextlib.contextmanager
def watchdog(seconds: int) -> Iterator[None]:
    """Fail instead of hanging when a daemon that should have refused is running."""

    def expired(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError(f"still running after {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


# --------------------------------------------------------------- the lock itself


def test_a_free_home_is_claimed_and_its_directory_made(lock_file: Path) -> None:
    lock = InstanceLock(lock_file)
    assert lock.try_acquire()
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock_file.parent.stat().st_mode) == 0o700
    lock.release()
    # The file stays: a lock on a file somebody has since unlinked guards nothing.
    assert lock_file.exists()


def test_a_held_home_is_refused_until_it_is_released(lock_file: Path) -> None:
    holder, other = InstanceLock(lock_file), InstanceLock(lock_file)
    assert holder.try_acquire()
    assert holder.try_acquire(), "a holder must not fight itself"
    assert not other.try_acquire()
    holder.release()
    holder.release()
    assert other.try_acquire()
    other.release()


async def test_acquire_waits_for_a_holder_that_is_leaving(
    lock_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instance_lock, "POLL_SECONDS", 0.01)
    holder, waiter = InstanceLock(lock_file), InstanceLock(lock_file)
    assert holder.try_acquire()
    asyncio.get_running_loop().call_later(0.1, holder.release)
    await asyncio.wait_for(waiter.acquire(), timeout=10)
    assert not is_free(lock_file), "the waiter holds it now"
    waiter.release()


@pytest.mark.usefixtures("quick_wait")
async def test_acquire_refuses_with_a_conflict_once_the_wait_is_over(lock_file: Path) -> None:
    holder = InstanceLock(lock_file)
    assert holder.try_acquire()
    with pytest.raises(RcError) as caught:
        await InstanceLock(lock_file).acquire()
    assert caught.value.code == "conflict"
    assert "another rc-client is already running for this home" in caught.value.message
    assert str(lock_file) in caught.value.message
    assert not is_free(lock_file), "the refusal must not disturb the holder"
    holder.release()


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_the_kernel_drops_the_lock_when_its_holder_dies(
    lock_file: Path, sig: signal.Signals
) -> None:
    with a_holder_process(lock_file) as holder:
        assert not is_free(lock_file)
        holder.send_signal(sig)
        assert holder.wait(timeout=30) == -sig
    assert is_free(lock_file)


def test_a_child_does_not_keep_the_home_locked_after_the_daemon_lets_go(lock_file: Path) -> None:
    holder = InstanceLock(lock_file)
    assert holder.try_acquire()
    # `close_fds=False` hands the child every descriptor that may be inherited, so
    # this tests the lock's own descriptor rather than subprocess's default.
    command = [sys.executable, "-c", "import time; time.sleep(120)"]
    with subprocess.Popen(command, close_fds=False) as child:
        try:
            holder.release()
            assert is_free(lock_file)
        finally:
            child.kill()


def test_each_home_has_its_own_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held: dict[str, InstanceLock] = {}
    for name in ("one", "two"):
        monkeypatch.setenv("RC_CLIENT_HOME", str(tmp_path / name))
        assert lock_path() == tmp_path / name / "state" / "daemon.lock"
        held[name] = InstanceLock(lock_path())
        assert held[name].try_acquire(), f"{name} must not be kept out by another home"
    monkeypatch.setenv("RC_CLIENT_HOME", str(tmp_path / "one"))
    assert not is_free(lock_path())
    for lock in held.values():
        lock.release()


# ------------------------------------------------------------------- the daemon


def listen(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    return listener


def reachable(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX) as client:
        try:
            client.connect(str(path))
        except OSError:
            return False
    return True


@pytest.mark.usefixtures("quick_wait")
def test_a_second_daemon_exits_with_failure_and_leaves_the_sockets_alone(
    socket_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RC_CLIENT_HOME", str(socket_dir / "home"))
    save_config(CONFIG)
    first = InstanceLock(lock_path())
    assert first.try_acquire()
    sockets = [channel_paths.socket_path(), pi_paths.socket_path()]
    listeners = [listen(path) for path in sockets]
    inodes = [path.stat().st_ino for path in sockets]
    try:
        with watchdog(30):
            assert main(["run"]) == EXIT_FAILURE
        assert "another rc-client is already running for this home" in capsys.readouterr().err
        assert [path.stat().st_ino for path in sockets] == inodes
        assert all(reachable(path) for path in sockets)
        assert not is_free(lock_path())
    finally:
        for listener in listeners:
            listener.close()
        first.release()


class Proceeded(Exception):
    """Raised by the first step after the claim, so `run` goes no further."""


@pytest.fixture
def stop_after_the_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    def proceed() -> None:
        raise Proceeded

    monkeypatch.setattr(daemon_module, "scrub_parent_secrets", proceed)


@pytest.fixture
async def leaving_daemon() -> InstanceLock:
    """A daemon that holds this home and lets go of it a moment from now."""
    holder = InstanceLock(lock_path())
    assert holder.try_acquire()
    asyncio.get_running_loop().call_later(0.3, holder.release)
    return holder


@pytest.mark.usefixtures("stop_after_the_claim")
async def test_a_daemon_started_beside_a_leaving_one_waits_and_then_holds_the_home(
    leaving_daemon: InstanceLock,
) -> None:
    daemon = Daemon(CONFIG)
    try:
        with pytest.raises(Proceeded):
            await asyncio.wait_for(daemon.run(), timeout=10)
        assert not leaving_daemon.held
        assert daemon.instance_lock.held
        assert not is_free(lock_path())
    finally:
        daemon.instance_lock.release()
        daemon.registry.close()


async def test_the_home_stays_locked_until_the_sockets_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon = Daemon(CONFIG)
    assert daemon.instance_lock.try_acquire()
    locked_when_stopping: list[bool] = []

    def watch(server: Any) -> None:
        stop = server.stop

        async def stop_while_watching() -> None:
            locked_when_stopping.append(not is_free(lock_path()))
            await stop()

        monkeypatch.setattr(server, "stop", stop_while_watching)

    # Each of these unlinks its socket as it stops, which would take the
    # socket of a daemon that had already been let in.
    watch(daemon.attach)
    watch(daemon.pi_socket)
    await daemon.shutdown()
    assert locked_when_stopping == [True, True]
    assert is_free(lock_path())


@pytest.fixture
def filesystem_without_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(fd: int, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", refuse)


@pytest.mark.usefixtures("stop_after_the_claim", "filesystem_without_locks")
async def test_a_filesystem_that_cannot_lock_does_not_keep_the_daemon_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    daemon = Daemon(CONFIG)
    try:
        with pytest.raises(Proceeded):
            await daemon.run()
    finally:
        daemon.registry.close()
    assert not daemon.instance_lock.held
    assert "single-instance lock unavailable" in [r.getMessage() for r in caplog.records]
