"""Close for a session a terminal holds: ending its CLI from an app (A61).

`session.archive` with `archived: true` on a `terminal` or `shared` row asks the
device to quit the agent in that terminal. The agent's own adapter decides how,
and whether it can at all: a CLI it cannot tell apart from another one, or one an
IDE runs in a panel, is `unsupported` and nothing is touched. Otherwise the turn
is interrupted, the CLI ended, and the device waits at most `CLOSE_WAIT` for the
process to be gone. Only then is the session filed, in one publish: `archived`,
`control: "none"` and `state: "stopped"` together, exactly as A39's close ends.
A CLI still there at the deadline is `conflict`, and nothing is published.

While a close runs, what the CLI going away would normally say — a bridge
closing, a scan that finds no holder, a pi extension saying goodbye, a daemon
or leader table — says nothing of the session: `SessionEntry.ending` holds it
back, so the one publish at the end is the whole story.

The parts every adapter shares are here: what the process to end must look like
before it is signalled — alive, this user's, on a terminal, the program it should
be — and the signal itself, SIGTERM through `procscan.terminate`, never SIGKILL.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from .. import exits, procscan
from ..errors import RcError
from ..logging_setup import logger
from ..procscan import Proc
from .queue import release

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ..agents.registry import CloseSpec
    from .hub import SessionEntry, SessionHub

log = logger("rc_client.ending")

# How long the CLI has to quit once it has been asked to.
CLOSE_WAIT = 10.0
CANNOT = "this session can't be closed from here; close it in its terminal"
TOO_SLOW = "the terminal did not quit in time; try again in a moment"
UNREADABLE = "the process table could not be read; try again in a moment"

Expect = Callable[[Proc], bool]


def holds_terminal(entry: SessionEntry) -> bool:
    """Whether a terminal holds the session, so Close has to end its CLI (A61)."""
    return entry.shared is not None or entry.session.control in {"terminal", "shared"}


async def close_terminal(hub: SessionHub, entry: SessionEntry) -> None:
    """End the CLI a terminal runs the session in, then file the session, or refuse.

    Under the session's lock, so no message is injected or typed while the CLI is
    being ended. `unsupported` and `conflict` leave the session as it was.
    """
    from ..agents.registry import CloseSpec, close_terminal_in_agent

    session = entry.session
    info = hub.agent_info(session.agent)
    if "close_terminal" not in info.capabilities:
        raise RcError("unsupported", CANNOT)
    loop = asyncio.get_running_loop()
    async with entry.lock:
        entry.ending = True
        entry.channel.closing = True
        try:
            spec = CloseSpec(
                entry=entry,
                shared=hub.shared,
                deadline=loop.time() + CLOSE_WAIT,
                process_ended=hub.process_ended,
                codex_daemon=hub.codex_daemon,
                grok_leader=hub.grok_leader,
            )
            ended = await close_terminal_in_agent(session.agent, spec)
            if not ended:
                log.warning(
                    "a terminal CLI did not quit in time after a close", agent=session.agent
                )
                raise RcError("conflict", TOO_SLOW)
            await _file(hub, entry)
        finally:
            entry.ending = False
            entry.channel.closing = False
    await entry.channel.publish_summary()


async def _file(hub: SessionHub, entry: SessionEntry) -> None:
    """The CLI is gone: let go of everything held for it and record the close, unpublished yet.

    What the device held for the terminal goes without a word: its prompts
    expire, the attachment is dropped. A runner it kept on somebody else's
    process ends the way A39 ends one — a Codex thread the device created is
    archived in Codex, a terminal's is only let go of (A58), a Grok session is
    unloaded from the leader — now that no terminal is in it. Messages held for
    the session are discarded, never sent (A39).
    """
    session = entry.session
    if entry.shared is not None:
        await hub.shared.ended(entry)
    runner, entry.runner = entry.runner, None
    session.control = "none"
    if runner is not None:
        try:
            await runner.shutdown()
        except Exception as exc:
            log.warning("letting go of a closed terminal session failed", error=str(exc)[:200])
    entry.holder_pid = None
    entry.holder_identity = None
    session.held = False
    session.archived = True
    session.state = "stopped"
    session.state_detail = None
    session.turn = None
    held, entry.queue = entry.queue, []
    for item in held:
        await release(item)
    if held:
        await entry.channel.publish_queue(hub.queue_snapshot(entry))


async def the_process(pid: int | None, expect: Expect) -> Proc | None:
    """The live process `pid` when it is one this device may end, or None when it is gone.

    `unsupported` for one that is not the CLI it should be: another program, one
    on no terminal — an IDE runs the CLI in a panel without one — or another
    user's. An unknown pid is not a process the device can name, so the session
    cannot be closed from here either.
    """
    if pid is None or pid <= 0:
        raise RcError("unsupported", CANNOT)
    scan = await procscan.scan_processes()
    if not scan.complete:
        raise RcError("conflict", UNREADABLE)
    proc = next((candidate for candidate in scan.procs if candidate.pid == pid), None)
    if proc is None:
        return None
    if not proc.has_tty or not expect(proc):
        raise RcError("unsupported", CANNOT)
    if await asyncio.to_thread(procscan.process_uid, pid) != os.getuid():
        raise RcError("unsupported", CANNOT)
    return proc


async def terminate(spec: CloseSpec, proc: Proc, expect: Expect) -> bool:
    """SIGTERM the process and wait for it until the deadline; whether it is gone by then.

    `procscan.terminate` checks identity, owner and shape once more right before
    the signal, and never sends SIGKILL. A process it would not signal because it
    is gone by then is gone all the same.
    """
    left = max(0.0, spec.deadline - asyncio.get_running_loop().time())
    ended = await procscan.terminate(proc.pid, proc.identity, deadline=left, expect=expect)
    return ended or not exits.alive(proc.pid)


async def wait(spec: CloseSpec, pid: int) -> bool:
    """Whether the process is gone by the close's deadline."""
    left = spec.deadline - asyncio.get_running_loop().time()
    return await spec.process_ended(pid, max(0.0, left))
