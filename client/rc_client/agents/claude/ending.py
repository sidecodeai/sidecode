"""Ending a Claude Code CLI a terminal runs a session in, on Close from an app (A61).

The CLI is the one the bridge registered, for a session the device is attached
to, or the one the holder scan paired with it. An idle CLI with an empty prompt
box is quit as the person would quit it, `/exit` typed into the terminal the
device's shim owns; any other gets Escape for a running turn (A42) and SIGTERM.
A CLI an IDE runs in a panel is not the device's to end: it has no terminal of
its own, and Claude Code's own record of it names another entrypoint than `cli`.
"""

from __future__ import annotations

import asyncio

from ...errors import RcError
from ...logging_setup import logger
from ...procscan import Proc
from ...sessions import ending
from ...sessions.typist import Typist
from ..registry import CloseSpec
from . import live
from .holders import looks_like_claude

log = logger("rc_client.claude.ending")

TERMINAL_ENTRYPOINT = "cli"


async def close_terminal(spec: CloseSpec) -> bool:
    entry = spec.entry
    state = entry.shared
    pid = state.attachment.pid if state is not None else entry.holder_pid
    proc = await ending.the_process(pid, looks_like_claude)
    if proc is None:
        return True
    if await _in_an_ide(proc):
        raise RcError("unsupported", ending.CANNOT)
    if state is not None and await _quit_typed(spec):
        log.info("typed /exit into a terminal Claude Code to close its session")
        return await ending.wait(spec, proc.pid)
    if state is not None:
        await _escape(spec)
    return await ending.terminate(spec, proc, looks_like_claude)


async def _in_an_ide(proc: Proc) -> bool:
    """Whether Claude Code's own record says this CLI was not started at a terminal."""
    records = await asyncio.to_thread(live.read_records)
    record = live.verified(records, proc.pid, proc.start)
    return record is not None and record.entrypoint not in ("", TERMINAL_ENTRYPOINT)


async def _quit_typed(spec: CloseSpec) -> bool:
    """`/exit` into the terminal the shim owns, when its CLI is idle with nothing typed."""
    link = spec.shared.link_for(spec.entry)
    if link is None:
        return False
    return await Typist(spec.entry, link, spec.shared).exit()


async def _escape(spec: CloseSpec) -> None:
    """Stop a running turn first, as the person would (A42); a dialog on screen is no turn."""
    link = spec.shared.link_for(spec.entry)
    if link is None:
        return
    try:
        await Typist(spec.entry, link, spec.shared).interrupt()
    except RcError:
        return
