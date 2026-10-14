"""The six `terminal.*` requests, and the four terminals a device may run (A38).

`terminal.open`, `input`, `resize`, `attach` and `close` come from an app;
`terminal.detach` comes from the gateway itself when the app connection holding
a terminal is gone (PROTOCOL.md 7.3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Any

from ..errors import RcError
from ..logging_setup import logger
from .shell import Publish, Terminal

log = logger("rc_client.terminal")

MAX_TERMINALS = 4
MAX_COLS = 500
MAX_ROWS = 200
MAX_INPUT_BYTES = 64 * 1024
DETACHED_KEEP_ALIVE_SECONDS = 600.0

NO_TERMINAL = "this device offers no terminal"


class TerminalManager:
    """Every terminal this device runs, keyed by its id."""

    def __init__(
        self,
        publish: Publish,
        *,
        enabled: bool = True,
        keep_alive: float = DETACHED_KEEP_ALIVE_SECONDS,
    ) -> None:
        self._publish = publish
        self.enabled = enabled
        # How long a detached shell is kept for an `attach`; a test shortens it.
        self.keep_alive = keep_alive
        self._terminals: dict[str, Terminal] = {}

    @property
    def count(self) -> int:
        return len(self._terminals)

    # -------------------------------------------------------------- requests

    async def open(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        holder = _holder(params)
        cols, rows = _size(params)
        if len(self._terminals) >= MAX_TERMINALS:
            raise RcError("conflict", f"this device already runs {MAX_TERMINALS} terminals")
        terminal = Terminal(
            holder=holder,
            cols=cols,
            rows=rows,
            publish=self._publish,
            on_gone=self._forget,
        )
        try:
            terminal.start()
        except OSError as exc:
            raise RcError("internal", f"no pseudo-terminal: {type(exc).__name__}") from exc
        self._terminals[terminal.terminal_id] = terminal
        log.info("terminal opened", terminal=terminal.terminal_id, cols=cols, rows=rows)
        return {"terminal_id": terminal.terminal_id}

    async def input(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        terminal = self._require(params)
        terminal.write(_bytes(params.get("data")))
        return {}

    async def resize(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        terminal = self._require(params)
        cols, rows = _size(params)
        terminal.resize(cols, rows)
        return {}

    async def attach(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        terminal = self._require(params)
        return terminal.attach(_holder(params))

    async def close(self, params: dict[str, Any]) -> dict[str, Any]:
        """End the shell. Idempotent: an id that is already gone is no error."""
        self._require_enabled()
        terminal = self._find(params)
        if terminal is not None:
            terminal.close()
        return {}

    async def detach(self, params: dict[str, Any]) -> dict[str, Any]:
        """The holder is gone: stop streaming, keep the shell (gateway only)."""
        self._require_enabled()
        terminal = self._find(params)
        if terminal is not None:
            terminal.detach(self.keep_alive)
        return {}

    # ------------------------------------------------------------- lifecycle

    async def stop(self) -> None:
        """Every terminal ends when the daemon does (7.3)."""
        terminals = list(self._terminals.values())
        self._terminals.clear()
        if terminals:
            await asyncio.gather(*(one.shutdown() for one in terminals), return_exceptions=True)

    def _forget(self, terminal: Terminal) -> None:
        self._terminals.pop(terminal.terminal_id, None)

    # ----------------------------------------------------------------- reads

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RcError("unsupported", NO_TERMINAL)

    def _find(self, params: dict[str, Any]) -> Terminal | None:
        terminal_id = params.get("terminal_id")
        if not isinstance(terminal_id, str):
            return None
        return self._terminals.get(terminal_id)

    def _require(self, params: dict[str, Any]) -> Terminal:
        terminal = self._find(params)
        if terminal is None:
            raise RcError("not_found", "no such terminal")
        return terminal


def _holder(params: dict[str, Any]) -> str:
    """The app connection that asked, which output is addressed to."""
    holder = params.get("from")
    if not isinstance(holder, str) or not holder:
        raise RcError("bad_request", "this request names no app connection")
    return holder


def _whole(value: Any, limit: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
        raise RcError("bad_request", f"{field} must be between 1 and {limit}")
    return value


def _size(params: dict[str, Any]) -> tuple[int, int]:
    return _whole(params.get("cols"), MAX_COLS, "cols"), _whole(
        params.get("rows"), MAX_ROWS, "rows"
    )


def _bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise RcError("bad_request", "data must be base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RcError("bad_request", "data must be base64") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise RcError("bad_request", f"input is larger than {MAX_INPUT_BYTES} bytes")
    return raw
