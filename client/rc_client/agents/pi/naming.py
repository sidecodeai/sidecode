"""Amendment A55: rename a pi session by whichever route its state allows.

pi names a session with a `session_info` entry in its file, and tells its own
extensions and RPC clients with `session_info_changed`. Who may write that entry
depends on who holds the session:

- an RPC child this device started is asked with `set_session_name`;
- a pi in a terminal is asked through the device's extension, with `set_name`,
  which calls `pi.setSessionName`, the same call `/name` ends in;
- a session no process holds has the entry appended to its file;
- a session a terminal holds that the device cannot reach is left to that
  terminal, since an entry appended behind a running pi's back is a side branch.

A pi started before this build of the extension was installed still runs the old
copy, which answers `set_name` as an unknown command: it has to be restarted.
"""

from __future__ import annotations

import asyncio

from ...errors import RcError
from ..registry import RenameSpec
from . import frames, session_file
from .adapter import PiRunner
from .terminal import PiTerminalSession

# What an extension that predates `set_name` answers. It carries no version the
# device could read, so this is how a stale copy is told from one that refused
# the name.
UNKNOWN_COMMAND = f"unknown command {frames.SET_NAME}"


async def rename_session(spec: RenameSpec) -> str:
    """Name the session, and answer the name pi now holds."""
    runner = spec.entry.runner
    try:
        if isinstance(runner, PiRunner) and runner.alive:
            return await runner.set_name(spec.name)
        if isinstance(runner, PiTerminalSession) and runner.link.alive:
            return await runner.set_name(spec.name)
    except RcError as exc:
        refusal = _refusal(exc)
        if refusal is exc:
            raise
        raise refusal from exc
    if spec.session.control == "terminal":
        raise RcError("unsupported", "rename it in the terminal")
    await asyncio.to_thread(session_file.name_session, spec.session.session_id, spec.name)
    return spec.name


def _refusal(error: RcError) -> RcError:
    """What a running pi's answer means for a rename, said in words of our own."""
    if error.code != "bad_request":
        return error
    if error.message == UNKNOWN_COMMAND:
        return RcError("unsupported", "restart pi to rename it")
    return RcError("bad_request", "pi does not accept this name")
