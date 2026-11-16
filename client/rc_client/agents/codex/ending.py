"""Ending the Codex TUI a terminal runs a session in, on Close from an app (A61).

A TUI with its own app-server writes its thread's rollout, so the rollout's one
writer is the process to end. A TUI on the shared daemon holds no rollout, and
the daemon says nothing of who is attached: the process the device can name is
the TUI whose command line resumes the thread by its id, or else the one Codex
TUI running in the thread's directory, and with two or more there it cannot
tell which, so the session is not its to close. A running turn is interrupted
first. The thread itself is never archived for a TUI the terminal started: it
stays in `codex resume`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re

from ...errors import RcError
from ...procscan import Proc, runs_one_of
from ...sessions import ending
from ..registry import CloseSpec
from . import rollouts
from .daemon import terminals
from .daemon.session import CodexDaemonSession

# How long the interrupted turn may take to settle before the TUI is ended anyway.
INTERRUPT_WAIT = 3.0
# A thread id as `codex resume <id>` takes it.
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
    """The TUI the thread is in, as far as the device can tell exactly, checked before it is ended.

    A TUI started with `codex resume <id>` names its thread on its command line,
    and one naming this thread is the one, wherever it runs. Otherwise the one
    TUI in the thread's directory is taken, unless it names another thread; with
    none there, or several, the device cannot say which it is.
    """
    scan = await terminals.scan_terminals()
    if not scan.complete:
        raise RcError("conflict", ending.UNREADABLE)
    thread_id = spec.session.session_id
    every = [proc for procs in scan.tuis.values() for proc in procs]
    named = [proc for proc in every if thread_id in proc.argv]
    if len(named) > 1:
        raise RcError("unsupported", ending.CANNOT)
    only = named[0] if named else scan.only(spec.session.cwd)
    if only is None or (not named and _names_a_thread(only)):
        raise RcError("unsupported", ending.CANNOT)
    return await ending.the_process(only.pid, terminals.looks_like_a_tui)


def _names_a_thread(proc: Proc) -> bool:
    """Whether a TUI's command line resumes a thread by its id."""
    return any(THREAD_ID.fullmatch(token) for token in proc.argv[1:])
