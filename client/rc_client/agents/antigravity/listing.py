"""Antigravity's own list of the person's conversations, read by an `agy acp` of the device's (A64).

The list process opens no conversation: it says who the device is and asks `session/list`, in the
empty folder of the device's own. `agy` lists every conversation of the account's store, whatever
client wrote it, with its directory, title and the time of its last write, unsorted and unpaged; a
conversation it kept without a directory comes back under the list process's own folder, which is
how the device tells one apart. `Authentication required` is how `agy` says nobody is signed in.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from . import errors, runtime
from .acp import AgyConnection, AgyError

log = logger("rc_client.antigravity.listing")

LIST_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class Listed:
    """One conversation as `agy`'s list gives it."""

    session_id: str
    cwd: str
    title: str
    updated_ms: int
    no_directory: bool = False


def parse(result: dict[str, Any], own: str) -> dict[str, Listed]:
    """Each conversation of one `session/list` answer, by id; `own` is the list process's folder."""
    sessions = result.get("sessions")
    found: dict[str, Listed] = {}
    for item in sessions if isinstance(sessions, list) else []:
        if not isinstance(item, dict):
            continue
        session_id = item.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        cwd = str(item.get("cwd") or "")
        found[session_id] = Listed(
            session_id=session_id,
            cwd=cwd,
            title=str(item.get("title") or ""),
            updated_ms=_instant(item.get("updatedAt")),
            no_directory=not cwd or os.path.realpath(cwd) == os.path.realpath(own),
        )
    return found


def _instant(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1000) if parsed.tzinfo is not None else 0


class Lister:
    """The device's list process, started when a list is wanted and kept between lists."""

    def __init__(self) -> None:
        self._agy: AgyConnection | None = None
        self._running: tuple[str, float] | None = None
        self._lock = asyncio.Lock()
        # Whether `agy` answered the last list with `Authentication required`; None until it did.
        self.signed_out: bool | None = None
        self.listed: dict[str, Listed] = {}

    async def fetch(self, binary: str) -> dict[str, Listed]:
        """`agy`'s whole list now, kept as `listed`; the refusal an app is told when it has none."""
        async with self._lock:
            own = str(runtime.lists_dir())
            try:
                agy = await self._connection(binary, own)
                answer = await agy.request("session/list", {}, timeout=LIST_TIMEOUT)
            except AgyError as exc:
                if exc.rpc_code == errors.AUTH_REQUIRED:
                    self.signed_out = True
                await self._drop()
                raise errors.refusal(exc.rpc_code, exc.message) from exc
            except RcError:
                await self._drop()
                raise
            except OSError as exc:
                await self._drop()
                raise RcError(
                    "agent_unavailable", "could not start agy to list its conversations"
                ) from exc
            self.signed_out = False
            self.listed = parse(answer, own)
            return self.listed

    async def _connection(self, binary: str, own: str) -> AgyConnection:
        """The running list process, or a new one when there is none or `agy` was replaced."""
        stamp = (binary, _mtime(binary))
        agy = self._agy
        if agy is not None and agy.alive and self._running == stamp:
            return agy
        await self._drop()
        agy = AgyConnection(binary, cwd=own, env=sanitized_child_env())
        self._agy, self._running = agy, stamp
        await agy.start()
        return agy

    async def _drop(self) -> None:
        agy, self._agy, self._running = self._agy, None, None
        if agy is not None:
            await agy.close()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


def _mtime(path: str) -> float:
    """When `agy` was last replaced: it updates itself in place for the processes to come."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0
