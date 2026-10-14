"""The pseudo-terminals the device can type into, one per attached CLI (A40).

The shim starts every attachable Claude Code inside `rc_client.channel.pty`,
which dials the same socket the channel bridge does and names the CLI it wraps.
That pid is the join: the bridge registers with `os.getppid()`, which is the
same process, so a session's attachment and its terminal find each other
without either of them knowing about the other.

A link is a request/response channel and nothing more. Every request carries an
id and is answered under it; a proxy that has gone quiet times out rather than
holding a person's phone, because the terminal is the one thing here the device
does not control.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from itertools import count
from typing import Any

from ..channel import wire
from ..errors import RcError
from ..logging_setup import logger

log = logger("rc_client.pty")

# Long enough for a terminal that is redrawing, short enough that a phone is
# never left waiting on a proxy that has stopped answering.
REQUEST_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class TerminalState:
    """What the proxy says about its terminal right now.

    `draft` counts the characters the person has typed since they last finished
    a line, `idle_for` is how long the keyboard has been quiet, and `text` is
    the screen as the proxy keeps it — empty unless the screen was asked for.
    """

    draft: int
    idle_for: float
    text: str = ""


class PtyLink:
    """One live pseudo-terminal proxy: the keystrokes go here."""

    def __init__(self, pid: int, writer: asyncio.StreamWriter) -> None:
        self.pid = pid
        self._writer: asyncio.StreamWriter | None = writer
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = count(1)

    @property
    def alive(self) -> bool:
        return self._writer is not None

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        """Type this into the terminal, exactly as the person's keyboard would.

        `clear` forgets the screen first, so a script reads what its own
        command drew rather than whatever was left from the last one.
        """
        return _read_state(await self._request(lambda rid: wire.keys(rid, data, clear)))

    async def screen(self) -> TerminalState:
        """What the terminal has printed, with the escape sequences taken out."""
        return _read_state(await self._request(wire.screen_request))

    async def state(self) -> TerminalState:
        """Whether anyone is typing there, without reading the screen."""
        return _read_state(await self._request(wire.state_request))

    async def _request(self, build: Any) -> dict[str, Any]:
        writer = self._writer
        if writer is None:
            raise RcError("conflict", "this terminal is no longer attached")
        request_id = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            writer.write(wire.encode(build(request_id)))
            await writer.drain()
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT)
        except (OSError, RuntimeError) as exc:
            self.detach()
            raise RcError("conflict", "this terminal is no longer attached") from exc
        except TimeoutError as exc:
            raise RcError("conflict", "the terminal did not answer in time") from exc
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, message: dict[str, Any]) -> None:
        """One answer from the proxy, matched to the request that asked for it."""
        future = self._pending.pop(str(message.get("id") or ""), None)
        if future is not None and not future.done():
            future.set_result(message)

    def detach(self) -> None:
        writer, self._writer = self._writer, None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RcError("conflict", "this terminal is no longer attached"))
        self._pending.clear()
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


def _read_state(message: dict[str, Any]) -> TerminalState:
    try:
        draft = int(message.get("draft") or 0)
        idle_for = float(message.get("idle_for") or 0.0)
    except (TypeError, ValueError):
        draft, idle_for = 0, 0.0
    return TerminalState(
        draft=max(0, draft), idle_for=idle_for, text=str(message.get("text") or "")
    )


class PtyLinks:
    """Every terminal the device owns, by the pid of the CLI running in it."""

    def __init__(self) -> None:
        self._links: dict[int, PtyLink] = {}

    def add(self, link: PtyLink) -> None:
        previous = self._links.get(link.pid)
        if previous is not None and previous is not link:
            previous.detach()
        self._links[link.pid] = link
        log.info("terminal attached", pid=link.pid)

    def remove(self, link: PtyLink) -> None:
        if self._links.get(link.pid) is link:
            self._links.pop(link.pid, None)
            log.info("terminal detached", pid=link.pid)
        link.detach()

    def get(self, pid: int | None) -> PtyLink | None:
        if not pid:
            return None
        link = self._links.get(pid)
        return link if link is not None and link.alive else None

    def pids(self) -> list[int]:
        """The CLIs the device has a terminal for, for diagnostics and tests."""
        return sorted(self._links)

    def clear(self) -> None:
        for link in list(self._links.values()):
            link.detach()
        self._links.clear()
