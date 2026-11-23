"""Which `agy` processes the device did not start may have a conversation open (A64, 4.4).

`agy` keeps who holds a conversation in lock files of its own, which the device never reads, lists
or tries to lock, and tells an ACP client nothing; a terminal's command line names only the
conversation it started in, which `/resume`, `/switch`, `/fork` or `/new` inside the TUI leave
behind. So the device judges conservatively from the processes alone. A holder is any process whose
program is `agy` — by its program path, not by the name it was started under — other than the
device's own children and what they started: a TUI, a print-mode run, another client's `agy acp`.

- Rule 1. A holder names the conversation on its command line: `--conversation <id>` or
  `--conversation=<id>`.
- Rule 2. A holder's working directory is the conversation's directory, by real path.
- Rule 3. The conversation has no directory, and any holder runs at all.

A session the device created and still holds is held by rule 1 alone, so the person's terminals in
its directory leave it alone. A scan that cannot be completed says so: not knowing who holds a
conversation never reads as nobody holding it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field

from ... import procscan
from ...logging_setup import logger

log = logger("rc_client.antigravity.presence")

PROGRAM = "agy"
CONVERSATION_FLAG = "--conversation"
IS_LINUX = sys.platform.startswith("linux")
PROGRAM_TIMEOUT = 3.0


@dataclass(frozen=True, slots=True)
class Holder:
    pid: int
    conversation: str | None
    cwd: str | None = None


@dataclass(slots=True)
class Holders:
    """The holders one scan found; `complete` is False when the scan could not be finished.

    `cwds` is False when their working directories could not all be read, which leaves rule 2
    unknown rather than false.
    """

    found: list[Holder] = field(default_factory=list)
    complete: bool = True
    cwds: bool = True


@dataclass(frozen=True, slots=True)
class Place:
    """Where `agy`'s list puts a conversation: its directory, or none at all."""

    directory: str
    no_directory: bool = False


def conversation_of(argv: list[str]) -> str | None:
    """The conversation a command line names, in either spelling."""
    for index, word in enumerate(argv):
        if word == CONVERSATION_FLAG and index + 1 < len(argv):
            return argv[index + 1] or None
        if word.startswith(CONVERSATION_FLAG + "="):
            return word.split("=", 1)[1] or None
    return None


def is_agy(program: str | None, binary: str | None) -> bool:
    """Whether a process runs `agy`: its program is called so, or is the very file resolved."""
    if not program:
        return False
    if os.path.basename(program) == PROGRAM:
        return True
    return binary is not None and _real(program) == _real(binary)


def holds(session_id: str, place: Place, holders: Holders, *, exempt: bool) -> bool | None:
    """Whether the holders found may have this conversation open; None when the scan cannot say.

    `exempt` is a session the device created and still holds, held by rule 1 alone.
    """
    if any(holder.conversation == session_id for holder in holders.found):
        return True
    if not holders.complete:
        return None
    if exempt or not holders.found:
        return False
    if place.no_directory:
        return True
    if not holders.cwds:
        return None
    directory = _real(place.directory)
    return any(
        holder.cwd is not None and _real(holder.cwd) == directory for holder in holders.found
    )


async def scan(binary: str | None, *, cwds: bool) -> Holders:
    """Every holder now: pid, the conversation it names, and its directory when `cwds` asks."""
    table = await procscan.scan_processes()
    if not table.complete:
        log.warning("the process table could not be read to tell who holds an agy conversation")
        return Holders(complete=False)
    ours = procscan.descendants(table.procs, os.getpid())
    others = [proc for proc in table.procs if proc.pid not in ours]
    programs, complete = await _programs([proc.pid for proc in others])
    if not complete:
        return Holders(complete=False)
    found = [
        Holder(proc.pid, conversation_of(proc.argv))
        for proc in others
        if is_agy(programs.get(proc.pid), binary)
    ]
    if not cwds or not found:
        return Holders(found)
    where, read = await procscan.process_cwds([holder.pid for holder in found])
    if not read:
        return Holders(found, cwds=False)
    # A holder whose directory is missing from a complete read has just exited: it still counts
    # for rule 3 in this scan, and for rule 2 it is in no directory.
    return Holders(
        [Holder(holder.pid, holder.conversation, where.get(holder.pid)) for holder in found]
    )


async def _programs(pids: list[int]) -> tuple[dict[int, str], bool]:
    """The program each process runs, by its path rather than the name it was started under."""
    if IS_LINUX:
        return await asyncio.to_thread(_linux_programs, pids), True
    try:
        process = await asyncio.create_subprocess_exec(
            "ps",
            "-axww",
            "-o",
            "pid=,comm=",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=PROGRAM_TIMEOUT)
    except (OSError, TimeoutError):
        return {}, False
    if process.returncode != 0:
        return {}, False
    wanted = set(pids)
    programs: dict[int, str] = {}
    for line in out.decode("utf-8", "replace").splitlines():
        head, _, program = line.strip().partition(" ")
        if head.isdigit() and int(head) in wanted:
            programs[int(head)] = program.strip()
    return programs, True


def _linux_programs(pids: list[int]) -> dict[int, str]:
    programs: dict[int, str] = {}
    for pid in pids:
        try:
            programs[pid] = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
                    programs[pid] = handle.read().strip()
            except OSError:
                continue
    return programs


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))
