"""Process inspection used to detect terminal CLI sessions.

Ownership decisions fail closed: when a scan cannot be completed the caller
must treat the session as read-only rather than assume nobody owns it. macOS
needs `/usr/sbin/lsof` for per-process cwd and for open-file writers; Linux
reads `/proc` directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from .logging_setup import logger

log = logger("rc_client.procscan")

LSOF = "/usr/sbin/lsof"
SCAN_TIMEOUT = 3.0
IS_LINUX = sys.platform.startswith("linux")
# `ps` prints a start time in the local zone and the user's locale. Both are pinned, so a start
# time means the same to every run of the daemon, whatever the shell it was started from, and
# reads the way Claude Code writes the `procStart` of its own records (`agents/claude/live.py`).
START_ENV = {"TZ": "UTC", "LC_ALL": "C"}


@dataclass(frozen=True, slots=True)
class Proc:
    pid: int
    ppid: int
    start: str
    command: str
    # Whether the process is attached to a terminal. Defaults to true because
    # every ownership decision here fails closed: an unknown answer must not
    # read as "nobody is sitting in front of this".
    has_tty: bool = True

    @property
    def identity(self) -> tuple[int, str]:
        return (self.pid, self.start)

    @property
    def argv(self) -> list[str]:
        return self.command.split("\x00") if "\x00" in self.command else self.command.split()


@dataclass(slots=True)
class Scan:
    procs: list[Proc]
    complete: bool


@dataclass(frozen=True, slots=True)
class OpenPaths:
    """What one process has open: its working directory and its regular files."""

    cwd: str
    files: tuple[str, ...]


async def _run(
    *args: str, timeout: float = SCAN_TIMEOUT, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run one helper and read its output. A non-zero code means "do not know".

    A child that exits while the loop is still holding it raises
    `ProcessLookupError` from whichever call reaches it first, and that has to
    stay inside this function: it reaches the caller as an unfinished scan,
    which every caller already knows how to refuse, whereas an exception
    escaping here takes down the whole scan round with it. `env` is added to
    the environment the helper inherits.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **env} if env else None,
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        return 124, ""
    except ProcessLookupError:
        return 127, ""
    return process.returncode or 0, out.decode("utf-8", "replace")


def _linux_scan() -> Scan:
    procs: list[Proc] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                stat = handle.read().decode("utf-8", "replace")
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace").rstrip("\x00")
        except (OSError, ValueError):
            continue
        tail = stat.rsplit(")", 1)[-1].split()
        start = tail[19] if len(tail) > 19 else "0"
        # Field 7 of `/proc/<pid>/stat`, counted from the closing parenthesis:
        # the controlling terminal, zero when the process has none.
        tty = tail[4] if len(tail) > 4 else "0"
        if not cmdline:
            continue
        procs.append(
            Proc(
                pid=pid,
                ppid=int(tail[1]) if len(tail) > 1 else 0,
                start=start,
                command=cmdline,
                has_tty=tty.isdigit() and int(tty) != 0,
            )
        )
    return Scan(procs=procs, complete=True)


def _linux_start(pid: int) -> str | None:
    """The start marker `_linux_scan` gives one process, or None when it is gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            tail = handle.read().decode("utf-8", "replace").rsplit(")", 1)[-1].split()
    except (OSError, ValueError):
        return None
    return tail[19] if len(tail) > 19 else None


async def process_start(pid: int) -> str | None:
    """When one process started, as `Proc.start` says it, or None when it is gone or unreadable.

    The start time is what tells a process from a later one that was given its pid.
    """
    if IS_LINUX:
        return await asyncio.to_thread(_linux_start, pid)
    code, out = await _run("ps", "-o", "lstart=", "-p", str(pid), env=START_ENV)
    start = " ".join(out.split())
    return start if code == 0 and start else None


async def scan_processes() -> Scan:
    """Every visible process with its pid, ppid, start marker and command line."""
    if IS_LINUX:
        return await asyncio.to_thread(_linux_scan)
    code, out = await _run("ps", "-axww", "-o", "pid=,ppid=,lstart=,tty=,command=", env=START_ENV)
    if code != 0:
        return Scan(procs=[], complete=False)
    procs: list[Proc] = []
    for line in out.splitlines():
        # pid, ppid, the five words of `lstart`, the terminal, then the command.
        parts = line.split(maxsplit=8)
        if len(parts) < 9 or not parts[0].isdigit():
            continue
        procs.append(
            Proc(
                pid=int(parts[0]),
                ppid=int(parts[1]) if parts[1].isdigit() else 0,
                start=" ".join(parts[2:7]),
                command=parts[8],
                has_tty=parts[7] != "??",
            )
        )
    return Scan(procs=procs, complete=True)


def descendants(procs: list[Proc], root: int) -> set[int]:
    """Every pid below `root` in the process tree, `root` included."""
    children: dict[int, list[int]] = {}
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc.pid)
    found = {root}
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, []):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


async def process_cwds(pids: list[int]) -> tuple[dict[int, str], bool]:
    """Working directory per pid. The bool is False when the scan is incomplete."""
    if not pids:
        return {}, True
    if IS_LINUX:
        result: dict[int, str] = {}
        for pid in pids:
            try:
                result[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                continue
        return result, True
    code, out = await _run(
        LSOF, "-n", "-P", "-w", "-a", "-p", ",".join(str(pid) for pid in pids), "-d", "cwd", "-Fpn"
    )
    if code not in (0, 1):
        return {}, False
    cwds: dict[int, str] = {}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current is not None:
            cwds[current] = line[1:]
    return cwds, True


def _linux_fd_paths(pid: int) -> tuple[str, ...]:
    """Every regular file this process has open, read from `/proc/<pid>/fd`."""
    fd_dir = f"/proc/{pid}/fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return ()
    paths: list[str] = []
    for fd in fds:
        try:
            if not stat.S_ISREG(os.stat(f"{fd_dir}/{fd}").st_mode):
                continue
            paths.append(os.readlink(f"{fd_dir}/{fd}"))
        except OSError:
            continue
    return tuple(paths)


def _parse_open_paths(out: str) -> dict[int, OpenPaths]:
    """Read one `lsof -F pftn` report: `p<pid>`, then `f`, `t`, `n` per file.

    The working directory arrives as the file whose descriptor is `cwd`;
    everything else is kept only when `lsof` calls it a regular file, which
    leaves out the terminal, the sockets and the pipes.
    """
    cwds: dict[int, str] = {}
    files: dict[int, list[str]] = {}
    pid: int | None = None
    fd = ""
    kind = ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p" and value.isdigit():
            pid, fd, kind = int(value), "", ""
        elif tag == "f":
            fd, kind = value, ""
        elif tag == "t":
            kind = value
        elif tag == "n" and pid is not None:
            if fd == "cwd":
                cwds[pid] = value
            elif kind == "REG":
                files.setdefault(pid, []).append(value)
    return {
        found: OpenPaths(cwd=cwds.get(found, ""), files=tuple(files.get(found, ())))
        for found in sorted(set(cwds) | set(files))
    }


async def process_open_paths(pids: list[int]) -> tuple[dict[int, OpenPaths], bool]:
    """Working directory and open regular files per pid, in one pass.

    The bool is False when the scan is incomplete, exactly as `process_cwds`:
    not knowing what a process holds must never read as "it holds nothing".
    """
    if not pids:
        return {}, True
    if IS_LINUX:
        found: dict[int, OpenPaths] = {}
        for pid in pids:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = ""
            found[pid] = OpenPaths(cwd=cwd, files=_linux_fd_paths(pid))
        return found, True
    code, out = await _run(
        LSOF, "-n", "-P", "-w", "-p", ",".join(str(pid) for pid in pids), "-F", "pftn"
    )
    if code not in (0, 1):
        return {}, False
    return _parse_open_paths(out), True


@dataclass(frozen=True, slots=True)
class Writers:
    """Which processes hold which regular files open for writing, by the files' real paths.

    `complete` is False when the scan could not be finished, which says nothing about who
    writes what: a caller treats it as "unknown", never as "nobody".
    """

    by_path: dict[str, tuple[Proc, ...]]
    complete: bool

    def of(self, path: str) -> tuple[Proc, ...]:
        """The processes writing `path`, none when nobody the scan asked about is."""
        return self.by_path.get(os.path.realpath(path), ())


def runs_one_of(proc: Proc, names: frozenset[str]) -> bool:
    """Whether this process runs one of these programs.

    The program is the first word of the command line, or the second when the
    first is the interpreter a launcher script runs under (`node …/bin/codex`).
    """
    return any(os.path.basename(token) in names for token in proc.argv[:2])


async def file_writers(names: frozenset[str]) -> Writers:
    """Every regular file the processes running one of `names` hold open for writing, in one pass.

    One process table and one look at the open files of the processes it names, however many
    files the caller wants to know about: asking after each file on its own was a helper process
    per file per scan. The device's own processes are left out, its agent children included.
    """
    scan = await scan_processes()
    if not scan.complete:
        return Writers(by_path={}, complete=False)
    ours = descendants(scan.procs, os.getpid())
    candidates = {
        proc.pid: proc for proc in scan.procs if proc.pid not in ours and runs_one_of(proc, names)
    }
    if not candidates:
        return Writers(by_path={}, complete=True)
    written, complete = await _written_paths(sorted(candidates))
    if not complete:
        return Writers(by_path={}, complete=False)
    by_path: dict[str, list[Proc]] = {}
    for pid, paths in written.items():
        proc = candidates.get(pid)
        if proc is None:
            continue
        for path in set(paths):
            by_path.setdefault(os.path.realpath(path), []).append(proc)
    return Writers(by_path={path: tuple(procs) for path, procs in by_path.items()}, complete=True)


async def _written_paths(pids: list[int]) -> tuple[dict[int, list[str]], bool]:
    """The regular files each pid holds open for writing. The bool is False when unknown."""
    if IS_LINUX:
        return await asyncio.to_thread(_linux_written_paths, pids), True
    code, out = await _run(
        LSOF, "-n", "-P", "-w", "-p", ",".join(str(pid) for pid in pids), "-F", "pfatn"
    )
    if code not in (0, 1):
        return {}, False
    return _parse_written(out), True


def _parse_written(out: str) -> dict[int, list[str]]:
    """Read one `lsof -F pfatn` report: `p<pid>`, then `f`, `a`, `t`, `n` per file.

    Only a regular file opened for writing counts: `a` is `w` or `u`, and `t` is `REG`.
    """
    written: dict[int, list[str]] = {}
    pid: int | None = None
    access = kind = ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p" and value.isdigit():
            pid, access, kind = int(value), "", ""
        elif tag == "f":
            access, kind = "", ""
        elif tag == "a":
            access = value
        elif tag == "t":
            kind = value
        elif tag == "n" and pid is not None and access in {"w", "u"} and kind == "REG":
            written.setdefault(pid, []).append(value)
    return written


def _linux_written_paths(pids: list[int]) -> dict[int, list[str]]:
    """`/proc/<pid>/fd` and the open flags in `/proc/<pid>/fdinfo`, for each pid."""
    written: dict[int, list[str]] = {}
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            path = _linux_written_fd(pid, fd_dir, fd)
            if path is not None:
                written.setdefault(pid, []).append(path)
    return written


def _linux_written_fd(pid: int, fd_dir: str, fd: str) -> str | None:
    """The path of one descriptor when it is a regular file open for writing."""
    try:
        if not stat.S_ISREG(os.stat(f"{fd_dir}/{fd}").st_mode):
            return None
        with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as handle:
            flags_line = next((row for row in handle if row.startswith("flags:")), "flags: 0")
        flags = int(flags_line.split()[1], 8)
        if flags & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR):
            return None
        return os.readlink(f"{fd_dir}/{fd}")
    except (OSError, ValueError, IndexError):
        return None


def process_uid(pid: int) -> int | None:
    try:
        if IS_LINUX:
            return os.stat(f"/proc/{pid}").st_uid
        out = subprocess.run(
            ["ps", "-o", "uid=", "-p", str(pid)], capture_output=True, text=True, timeout=3
        )
        value = out.stdout.strip()
        return int(value) if value.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None


async def terminate(
    pid: int,
    identity: tuple[int, str],
    deadline: float = 3.0,
    expect: Callable[[Proc], bool] | None = None,
) -> bool:
    """SIGTERM one process after re-verifying its identity, owner and shape.

    Never signals a process group and never escalates to SIGKILL: the target is
    an interactive CLI that must be given the chance to flush its transcript.
    `identity` is `(pid, start)`, and macOS start times have one-second
    granularity, so `expect` re-checks that the pid is still the kind of
    process we meant to signal.
    """
    scan = await scan_processes()
    match = next((proc for proc in scan.procs if proc.pid == pid), None)
    if match is None or match.identity != identity:
        log.warning("takeover target vanished or was recycled", pid=pid)
        return False
    if expect is not None and not expect(match):
        log.warning("takeover target is no longer the expected process", pid=pid)
        return False
    if await asyncio.to_thread(process_uid, pid) != os.getuid():
        log.warning("refusing to signal a process owned by another user", pid=pid)
        return False
    try:
        os.kill(pid, 15)
    except OSError:
        return False
    waited = 0.0
    while waited < deadline:
        await asyncio.sleep(0.05)
        waited += 0.05
        try:
            os.kill(pid, 0)
        except OSError:
            return True
    return False
