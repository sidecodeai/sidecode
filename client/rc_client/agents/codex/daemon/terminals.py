"""Which Codex TUIs are alive on this machine, and in which directory.

The shared daemon announces nothing when a terminal TUI exits and offers no way
to ask who is attached to a thread, so the TUI process itself is the only
evidence there is. A directory is not an identity, though: the daemon never
unloads a thread, so a directory a person works in accumulates one loaded
thread per `codex` they have ever run there, and a single live TUI must not
speak for all of them. The scan therefore counts the TUIs per directory, and
the service hands out that many claims.

A TUI is classified by what it holds open, never by the flags it was started
with. A TUI running its own embedded app-server writes its thread's rollout
itself and is the process holding a file under `$CODEX_HOME/sessions`; one on
the shared daemon holds no rollout at all, because the daemon holds it. Only
the first kind is excluded from the count, and the rollout mirror keeps
publishing it read-only as it publishes any other terminal transcript.

Scans fail closed, exactly like the Claude holder scan: an incomplete answer
means "unknown", never "nobody".
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field

from ....procscan import Proc, descendants, process_open_paths, scan_processes
from ..runtime import SESSIONS_DIR

# Whole argv tokens that make a `codex` process something other than a TUI. They
# are matched anywhere in the command line because the helpers that Codex and
# the ChatGPT app spawn put `-c key=value` overrides before their subcommand.
NOT_A_TUI = frozenset({"app-server", "exec", "mcp", "mcp-server", "proto", "daemon"})


@dataclass(slots=True)
class TerminalScan:
    """How many Codex TUIs are running in each working directory, and which they are."""

    cwds: dict[str, int]
    complete: bool
    tuis: dict[str, tuple[Proc, ...]] = field(default_factory=dict)

    def count(self, cwd: str) -> int:
        """How many terminals this directory can speak for."""
        if not cwd:
            return 0
        return self.cwds.get(os.path.realpath(cwd), 0)

    def holds(self, cwd: str) -> bool:
        return self.count(cwd) > 0

    def only(self, cwd: str) -> Proc | None:
        """The one TUI in this directory, when there is exactly one (A61); None otherwise."""
        found = self.tuis.get(os.path.realpath(cwd), ()) if cwd else ()
        return found[0] if len(found) == 1 else None


def looks_like_a_tui(proc: Proc) -> bool:
    """Whether this process is a `codex` TUI, whatever flags it carries."""
    argv = proc.argv
    if not argv or not proc.has_tty:
        return False
    if os.path.basename(argv[0]) != "codex":
        return False
    return not any(token in NOT_A_TUI for token in argv[1:])


def holds_a_rollout(files: Iterable[str]) -> bool:
    """Whether these open files include a rollout, which only the writer has."""
    root = os.path.realpath(SESSIONS_DIR) + os.sep
    return any(os.path.realpath(path).startswith(root) for path in files)


async def scan_terminals() -> TerminalScan:
    """Every directory a live Codex TUI is running in, this device's own excluded."""
    scan = await scan_processes()
    if not scan.complete:
        return TerminalScan(cwds={}, complete=False)
    ours = descendants(scan.procs, os.getpid())
    candidates = [proc for proc in scan.procs if proc.pid not in ours and looks_like_a_tui(proc)]
    if not candidates:
        return TerminalScan(cwds={}, complete=True)
    opened, complete = await process_open_paths([proc.pid for proc in candidates])
    if not complete:
        return TerminalScan(cwds={}, complete=False)
    by_pid = {proc.pid: proc for proc in candidates}
    tuis: dict[str, list[Proc]] = {}
    for pid, held in opened.items():
        if held.cwd and not holds_a_rollout(held.files) and pid in by_pid:
            tuis.setdefault(os.path.realpath(held.cwd), []).append(by_pid[pid])
    return TerminalScan(
        cwds={cwd: len(procs) for cwd, procs in tuis.items()},
        complete=True,
        tuis={cwd: tuple(procs) for cwd, procs in tuis.items()},
    )
