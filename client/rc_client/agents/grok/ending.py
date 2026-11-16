"""Ending the Grok Build TUI a terminal runs a session in, on Close from an app (A61).

Grok's own registry names the process of every TUI while it runs, so that is the
one to end. A running turn is cancelled first (`session/cancel`); once the TUI is
gone, a session the device holds in the leader is unloaded there too
(`session/close`), which is safe now that no terminal is in it.
"""

from __future__ import annotations

import asyncio
import contextlib

from ...errors import RcError
from ...procscan import Proc, runs_one_of
from ...sessions import ending
from ..registry import CloseSpec
from . import sessions as grok_sessions
from .adapter import GrokRunner

# How long a cancelled turn may take to settle before the TUI is ended anyway.
INTERRUPT_WAIT = 3.0


def runs_grok(proc: Proc) -> bool:
    return runs_one_of(proc, grok_sessions.WRITERS)


async def close_terminal(spec: CloseSpec) -> bool:
    session_id = spec.session.session_id
    registered = await asyncio.to_thread(_registered, session_id)
    proc = await ending.the_process(registered, runs_grok)
    if proc is None:
        return True
    runner = spec.entry.runner
    if isinstance(runner, GrokRunner) and runner.attached and spec.session.turn is not None:
        with contextlib.suppress(TimeoutError, RcError):
            await asyncio.wait_for(runner.interrupt(), INTERRUPT_WAIT)
    return await ending.terminate(spec, proc, runs_grok)


def _registered(session_id: str) -> int | None:
    """The pid Grok's registry gives the TUI in this session, if it names one."""
    entries, known = grok_sessions.active_entries()
    if not known:
        return None
    found = [entry.pid for entry in entries if entry.session_id == session_id and entry.pid > 0]
    return found[0] if len(found) == 1 else None
