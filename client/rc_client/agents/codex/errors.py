"""The error a Codex app-server answers a request with."""

from __future__ import annotations

from typing import Any

from ...errors import RcError


class CodexError(RcError):
    """An error the app-server answered a request with, and the JSON-RPC code it gave.

    A request that failed because the connection did is a plain `RcError`.
    """

    def __init__(self, message: str, rpc_code: Any) -> None:
        super().__init__("agent_unavailable", message)
        exact = isinstance(rpc_code, int) and not isinstance(rpc_code, bool)
        self.rpc_code: int | None = rpc_code if exact else None
