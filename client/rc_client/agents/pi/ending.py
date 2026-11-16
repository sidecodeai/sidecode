"""Ending a pi a terminal runs a session in, on Close from an app (A61).

The device's extension runs inside that pi, so it is asked to shut the process
down the way pi does it, `ctx.abort()` then `ctx.shutdown()`. An extension from
before A61 does not know the command, and the pid its `hello` gave is ended
with SIGTERM instead.
"""

from __future__ import annotations

from ...errors import RcError
from ...logging_setup import logger
from ...procscan import Proc, runs_one_of
from ...sessions import ending
from ..registry import CloseSpec
from . import frames
from .terminal import PiTerminalSession

log = logger("rc_client.pi.ending")

PROGRAMS = frozenset({"pi"})
# How long the extension may take to say it is shutting pi down.
SHUTDOWN_REPLY = 3.0


def runs_pi(proc: Proc) -> bool:
    return runs_one_of(proc, PROGRAMS)


async def close_terminal(spec: CloseSpec) -> bool:
    runner = spec.entry.runner
    if not isinstance(runner, PiTerminalSession):
        raise RcError("unsupported", ending.CANNOT)
    proc = await ending.the_process(runner.link.hello.pid, runs_pi)
    if proc is None:
        return True
    if await _shut_down(runner):
        return await ending.wait(spec, proc.pid)
    return await ending.terminate(spec, proc, runs_pi)


async def _shut_down(runner: PiTerminalSession) -> bool:
    """Ask the extension to shut pi down; False when it cannot be asked, so a signal must do.

    The link going away is the answer as much as a reply is: pi shut down before
    its reply was written.
    """
    try:
        await runner.link.command(frames.SHUTDOWN, timeout=SHUTDOWN_REPLY)
    except RcError as exc:
        if exc.code == "agent_unavailable":
            return True
        log.info("the pi extension did not shut pi down; ending it with a signal", code=exc.code)
        return False
    return True
