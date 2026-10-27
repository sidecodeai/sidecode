"""Amendment A55: rename a Codex thread through the app-server's `thread/name/set`.

The app-server owns a thread's name. It appends the name to `session_index.jsonl`
and writes its state store, so asking it is the only safe way in, and it serves a
thread nothing has loaded (a cold thread goes straight to the store) as well as
one in the middle of a turn (a loaded thread is ordered with its rollout
writes). It then tells every client connected to it with `thread/name/updated`,
a terminal on the shared daemon included.

Which connection to ask depends on who holds the thread. A runner built while
the daemon was not up owns an app-server of its own; every other thread, held or
not, goes to the shared daemon. A terminal with an embedded app-server of its own
is connected to neither, so it is not told: the name is persisted, and that
terminal shows it once it reads the thread again (unverified).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...errors import RcError
from ..registry import RenameSpec
from .adapter import CodexRunner
from .errors import CodexError

METHOD = "thread/name/set"
# How long a rename waits for the app-server. The hub holds the session's lock
# for as long, which stops a message and the queue behind it, and the gateway
# answers the app `timeout` after 60 seconds: waiting as long as the transports
# do by default would keep the lock after the app has given up.
TIMEOUT = 15.0

# JSON-RPC codes the app-server answers with. The first is what it says of a
# thread it cannot name — archived, deleted, ephemeral — and the second of a
# method it does not have, as an app-server older than 0.145 does not.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601

Call = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]


async def rename_thread(spec: RenameSpec) -> str:
    """Name the thread, and answer the name Codex now holds, which is the one asked for."""
    call = _connection(spec)
    try:
        await call(METHOD, {"threadId": spec.session.session_id, "name": spec.name}, TIMEOUT)
    except CodexError as exc:
        raise _refusal(exc) from exc
    return spec.name


def _connection(spec: RenameSpec) -> Call:
    """The app-server to ask: the runner's own while it runs, else the shared daemon."""
    runner = spec.entry.runner
    if isinstance(runner, CodexRunner) and runner.connected:
        return runner.call
    daemon = spec.codex_daemon
    if daemon is not None and daemon.ready:
        return daemon.client.request
    if not spec.info.path:
        raise RcError("agent_unavailable", "codex is not installed on this device")
    raise RcError("unsupported", "the codex daemon is not connected; run rc-client codex setup")


def _refusal(error: CodexError) -> RcError:
    """What the app-server's answer means for a rename, said in words of our own.

    Its own words name the thread, and for a failure inside it a path.
    """
    if error.rpc_code == INVALID_REQUEST:
        return RcError("unsupported", "the codex thread is archived or no longer exists")
    if error.rpc_code == METHOD_NOT_FOUND:
        return RcError("unsupported", "this codex version cannot rename a thread")
    return RcError("agent_unavailable", "codex could not rename the thread")
