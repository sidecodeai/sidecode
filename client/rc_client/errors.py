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


class AgentStarting(RcError):
    """The agent a request acts through has not started yet, or its shared daemon is being reached.

    It answers `agent_unavailable`, and the device has done nothing, nor will it later: after a
    restart each agent starts in the background, and a request that cannot wait any longer for
    its agent is refused rather than carried out after the app has given up on it.
    """

    def __init__(self, agent: str) -> None:
        super().__init__(
            "agent_unavailable", f"{agent} is still starting on this device; try again in a moment"
        )
