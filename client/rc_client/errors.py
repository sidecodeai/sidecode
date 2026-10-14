"""Protocol error codes and the exception that carries them to a `reply`."""

from __future__ import annotations

from typing import Literal

ErrorCode = Literal[
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "device_offline",
    "agent_unavailable",
    "conflict",
    "timeout",
    "internal",
    "unsupported",
    "too_large",
]


class RcError(Exception):
    """An error that maps directly onto a protocol `reply` with `ok: false`."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
