"""Find the live `claude` process that owns a transcript.

Detection is process-scan first because an idle Claude TUI does not keep its
transcript file open. An incomplete scan means "unknown owner", which callers
must treat as read-only rather than as "nobody".
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from ...channel.shim import CHANNEL_FLAG, CHANNEL_VALUE
from ...procscan import Proc, descendants, process_cwds, scan_processes, terminate

_RESUME_FLAGS = {"--resume", "-r", "--session-id"}
_BACKGROUND_MARKERS = ("daemon", "bg-pty-host", "bg-spare", "app-server", "mcp")


@dataclass(slots=True)
class Holder:
    pid: int
    identity: tuple[int, str]
    session_id: str | None
    cwd: str | None
    # Started through the device's own shim, so it loads the channel and names
    # the session it is running the moment its bridge registers. Guessing for
    # one of these is never needed, and in the second before it registers it is
    # always wrong.
    attachable: bool = False


@dataclass(frozen=True, slots=True)
class SessionRef:
    """One session `assign` may find a terminal process for.

    `pid` and `identity` are what the device already knows about that session's
    process, from a channel bridge that registered it or from the previous scan.
    """

    session_id: str
    cwd: str | None = None
    pid: int | None = None
    identity: tuple[int, str] | None = None


def _real(path: str | None) -> str:
    return os.path.realpath(path) if path else ""


@dataclass(slots=True)
class HolderScan:
    holders: list[Holder]
    complete: bool

    def assign(self, sessions: Sequence[SessionRef]) -> dict[str, Holder]:
        """Match sessions to terminal processes, each process to one session at most.

        A process is claimed by name first: the session id its argv resumed, or
        the pid a channel bridge registered for a session. Those are the only
        two facts that identify which conversation a process is in. The working
        directory is a last resort and never decides between candidates —
        several sessions share a directory all the time, and handing the same
        process to every one of them made every one of them look like the
        session the terminal was actually running.
        """
        found: dict[str, Holder] = {}
        taken: set[int] = set()
        for ref in sessions:
            self._take(found, taken, ref.session_id, self._named(ref.session_id, taken))
        for ref in sessions:
            if ref.session_id not in found:
                self._take(found, taken, ref.session_id, self._known(ref, taken))
        self._take_by_cwd(sessions, found, taken)
        return found

    @staticmethod
    def _take(
        found: dict[str, Holder], taken: set[int], session_id: str, holder: Holder | None
    ) -> None:
        if holder is None:
            return
        found[session_id] = holder
        taken.add(holder.pid)

    def _named(self, session_id: str, taken: set[int]) -> Holder | None:
        """A process whose argv says which session it resumed."""
        return next(
            (
                holder
                for holder in self.holders
                if holder.session_id == session_id and holder.pid not in taken
            ),
            None,
        )

    def _known(self, ref: SessionRef, taken: set[int]) -> Holder | None:
        """The process this session was already paired with, if it is still there.

        The identity guard is what makes a recycled pid a miss rather than a
        stranger: macOS start times have one-second granularity, so the pair is
        only as good as both halves of it.
        """
        if ref.pid is None or ref.pid in taken:
            return None
        return next(
            (
                holder
                for holder in self.holders
                if holder.pid == ref.pid
                and (ref.identity is None or holder.identity == ref.identity)
            ),
            None,
        )

    def _take_by_cwd(
        self, sessions: Sequence[SessionRef], found: dict[str, Holder], taken: set[int]
    ) -> None:
        """The last resort, taken only where the pairing is the only one possible."""
        free = [
            holder
            for holder in self.holders
            if holder.session_id is None
            and not holder.attachable
            and holder.pid not in taken
            and holder.cwd
        ]
        waiting = [ref for ref in sessions if ref.session_id not in found and ref.cwd]
        for directory in {_real(holder.cwd) for holder in free}:
            here = [holder for holder in free if _real(holder.cwd) == directory]
            mine = [ref for ref in waiting if _real(ref.cwd) == directory]
            if len(here) == 1 and len(mine) == 1:
                self._take(found, taken, mine[0].session_id, here[0])


def _is_background(argv: list[str]) -> bool:
    """True for the helper processes Claude Code spawns, never for a TUI.

    Matching whole tokens rather than the joined command line matters: the
    device's own shim adds `--mcp-config <path>`, and a substring test would
    write off every session started through it as a background helper.
    """
    return any(token.lstrip("-").split("=", 1)[0] in _BACKGROUND_MARKERS for token in argv[1:])


def _is_attachable(argv: list[str]) -> bool:
    """True when the device's shim started this CLI, so it will name itself."""
    for index, token in enumerate(argv):
        if token == f"{CHANNEL_FLAG}={CHANNEL_VALUE}":
            return True
        if token == CHANNEL_FLAG and argv[index + 1 : index + 2] == [CHANNEL_VALUE]:
            return True
    return False


def _looks_like_claude(proc: Proc) -> bool:
    argv = proc.argv
    if not argv:
        return False
    if _is_background(argv):
        return False
    for token in argv[:3]:
        base = os.path.basename(token)
        if base == "claude" or base == "claude-code":
            return True
        if base == "cli.js" and "claude" in token:
            return True
    return False


def _session_from_argv(argv: list[str]) -> str | None:
    for index, token in enumerate(argv):
        if token in _RESUME_FLAGS and index + 1 < len(argv):
            candidate = argv[index + 1]
            if not candidate.startswith("-"):
                return candidate
        for flag in _RESUME_FLAGS:
            if token.startswith(f"{flag}="):
                return token.split("=", 1)[1]
    return None


async def scan_holders(exclude_pids: set[int] | None = None) -> HolderScan:
    scan = await scan_processes()
    if not scan.complete:
        return HolderScan(holders=[], complete=False)
    # Our own agent children look exactly like a terminal CLI, so exclude the
    # whole daemon subtree rather than just this process.
    excluded = set(exclude_pids or set()) | descendants(scan.procs, os.getpid())
    candidates = [
        proc for proc in scan.procs if proc.pid not in excluded and _looks_like_claude(proc)
    ]
    cwds, complete = await process_cwds([proc.pid for proc in candidates])
    holders = [
        Holder(
            pid=proc.pid,
            identity=proc.identity,
            session_id=_session_from_argv(proc.argv),
            cwd=cwds.get(proc.pid),
            attachable=_is_attachable(proc.argv),
        )
        for proc in candidates
    ]
    return HolderScan(holders=holders, complete=complete)


async def release_holder(pid: int, identity: tuple[int, str]) -> bool:
    """SIGTERM the exact terminal process so the session can be resumed."""
    return await terminate(pid, identity, expect=_looks_like_claude)
