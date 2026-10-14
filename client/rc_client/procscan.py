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


async def _run(*args: str, timeout: float = SCAN_TIMEOUT) -> tuple[int, str]:
    """Run one helper and read its output. A non-zero code means "do not know".

    A child that exits while the loop is still holding it raises
    `ProcessLookupError` from whichever call reaches it first, and that has to
    stay inside this function: it reaches the caller as an unfinished scan,
    which every caller already knows how to refuse, whereas an exception
    escaping here takes down the whole scan round with it.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
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


async def scan_processes() -> Scan:
    """Every visible process with its pid, ppid, start marker and command line."""
    if IS_LINUX:
        return await asyncio.to_thread(_linux_scan)
    code, out = await _run("ps", "-axww", "-o", "pid=,ppid=,lstart=,tty=,command=")
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


async def file_writers(path: str) -> tuple[list[int], bool]:
    """Pids holding `path` open for writing. The bool is False if unknown."""
    if IS_LINUX:
        try:
            target = os.stat(path)
        except OSError:
            return [], True
        writers: list[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    stat = os.stat(f"{fd_dir}/{fd}")
                    if stat.st_ino != target.st_ino or stat.st_dev != target.st_dev:
                        continue
                    with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as handle:
                        flags_line = next(
                            (row for row in handle if row.startswith("flags:")), "flags: 0"
                        )
                except (OSError, StopIteration, ValueError):
                    continue
                flags = int(flags_line.split()[1], 8)
                if flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                    writers.append(pid)
                break
        return writers, True
    code, out = await _run(LSOF, "-n", "-P", "-w", "-F", "pfan", "--", path)
    if code not in (0, 1):
        return [], False
    writers = []
    current: int | None = None
    access = ""
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
            access = ""
        elif line.startswith("a"):
            access = line[1:]
        elif line.startswith("n") and current is not None and access in {"w", "u"}:
            if os.path.realpath(line[1:]) == os.path.realpath(path):
                writers.append(current)
    return sorted(set(writers)), True


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
