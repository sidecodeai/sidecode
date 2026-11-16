"""Ending the Codex TUI a terminal runs a session in, on Close from an app (A61).

A TUI with its own app-server writes its thread's rollout, so the rollout's one
writer is the process to end. A TUI on the shared daemon holds no rollout, and
the daemon says nothing of who is attached: the only process the device can name
is the one Codex TUI running in the thread's directory, and with two or more
there it cannot tell which, so the session is not its to close. A running turn
is interrupted first. The thread itself is never archived for a TUI the
terminal started: it stays in `codex resume`.
"""

from __future__ import annotations

import asyncio
import contextlib

from ...errors import RcError
from ...procscan import Proc, runs_one_of
from ...sessions import ending
from ..registry import CloseSpec
from . import rollouts
from .daemon import terminals
from .daemon.session import CodexDaemonSession

# How long the interrupted turn may take to settle before the TUI is ended anyway.
INTERRUPT_WAIT = 3.0


def runs_codex(proc: Proc) -> bool:
    return runs_one_of(proc, rollouts.WRITERS)


async def close_terminal(spec: CloseSpec) -> bool:
    entry = spec.entry
    runner = entry.runner
    if isinstance(runner, CodexDaemonSession):
        proc = await _the_tui(spec)
        if proc is None:
            return True
        if runner.busy:
            # The turn runs in the daemon, not in the TUI: ending the TUI would not stop it.
            with contextlib.suppress(TimeoutError, RcError):
                await asyncio.wait_for(runner.interrupt(), INTERRUPT_WAIT)
        return await ending.terminate(spec, proc, terminals.looks_like_a_tui)
    proc = await ending.the_process(entry.holder_pid, runs_codex)
    if proc is None:
        return True
    if entry.holder_identity is not None and proc.identity != entry.holder_identity:
        raise RcError("unsupported", ending.CANNOT)
    return await ending.terminate(spec, proc, runs_codex)


async def _the_tui(spec: CloseSpec) -> Proc | None:
    """The one TUI in the thread's directory, checked as any process to end is."""
    scan = await terminals.scan_terminals()
    if not scan.complete:
        raise RcError("conflict", ending.UNREADABLE)
    only = scan.only(spec.session.cwd)
    if only is None:
        raise RcError("unsupported", ending.CANNOT)
    return await ending.the_process(only.pid, terminals.looks_like_a_tui)
