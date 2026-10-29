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


class TerminalNotReady(RcError):
    """A terminal CLI holds the session and the device cannot reach it yet, or cannot tell.

    It answers `timeout`, which an app may retry, and the device has done nothing: what it asked
    would have started a second Claude on a transcript a live terminal is writing.
    """

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)
