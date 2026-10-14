"""The one place an agent's usage-limit stop is read (amendment A35, 7.2).

Claude Code and Codex both end a turn when the vendor's five-hour or weekly
window is used up, and each says so in a signal of its own: Claude Code writes
an HTTP 429 row into the session transcript and reports the same status in the
SDK's result message, while Codex fails the turn with the error info
`usageLimitExceeded` and keeps the windows themselves behind
`account/rateLimits/read`. The vendor's sentence is never parsed — its wording
changes and it is translated — so everything here reads structured fields only.

Grok Build and pi report nothing the device can read, so their turns never carry
a `LimitStop` and no resume is scheduled for them. An agent that gains a signal
joins by adding a reader here, with no change to the wire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents.codex.account import parse_limits

# `AgentLimit.window_minutes` for the two windows the vendors run (PROTOCOL 4.2).
FIVE_HOURS = 300
SEVEN_DAYS = 10080
# What Claude Code calls each window. Anything else leaves the window unknown,
# which is a stop the scheduler still handles: it estimates the time instead.
CLAUDE_WINDOWS = {"five_hour": FIVE_HOURS, "seven_day": SEVEN_DAYS}
# The HTTP status a vendor refuses a request with when the window is used up.
RATE_LIMITED = 429
# `CodexErrorInfo::UsageLimitExceeded`, compared without case or underscores so
# that the app-server's camelCase and the core's snake_case both match.
CODEX_USAGE_LIMIT = "usagelimitexceeded"
# How much of a transcript's end is read when looking for the row that carries
# the reset time. A limit row is the last thing the CLI writes for that turn.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


@dataclass(slots=True)
class LimitStop:
    """Why a turn ended at the vendor's usage limit (PROTOCOL 5.9).

    `resets_at_ms` is null when the vendor named no time, and `window_minutes`
    absent when it named no window; both happen, and neither stops a resume
    from being scheduled.
    """

    window_minutes: int | None = None
    resets_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.window_minutes is not None:
            result["window_minutes"] = self.window_minutes
        result["resets_at"] = self.resets_at_ms
        return result


@dataclass(slots=True)
class TurnEnd:
    """How a turn ended, carried together because a limit stop is one of them."""

    stop_reason: str = "completed"
    limit: LimitStop | None = None


def _seconds_to_ms(value: Any) -> int | None:
    """Vendors count resets in Unix seconds; the wire counts in milliseconds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value * 1000)


def _is_rate_limited(row: dict[str, Any]) -> bool:
    """Whether a Claude Code transcript row is the CLI's own 429 record."""
    if not row.get("isApiErrorMessage"):
        return False
    if row.get("apiErrorStatus") == RATE_LIMITED:
        return True
    return str(row.get("error") or "") == "rate_limit"


def claude_row_limit(row: dict[str, Any]) -> LimitStop | None:
    """The limit a Claude Code transcript row reports, or None for any other row.

    The CLI files the stop as an assistant row marked `isApiErrorMessage`, whose
    `quotaLimits` names the window and the second it resets.
    """
    if row.get("type") != "assistant" or not _is_rate_limited(row):
        return None
    quota = row.get("quotaLimits")
    quota = quota if isinstance(quota, dict) else {}
    return LimitStop(
        window_minutes=CLAUDE_WINDOWS.get(str(quota.get("rateLimitType") or "")),
        resets_at_ms=_seconds_to_ms(quota.get("resetsAt")),
    )


def claude_result_limit(api_error_status: Any) -> LimitStop | None:
    """The limit the SDK's result message reports: that there is one, and no more.

    `ResultMessage` carries the HTTP status and not the reset time, which the
    CLI writes into the session's own transcript; `claude_transcript_limit`
    reads it from there.
    """
    return LimitStop() if api_error_status == RATE_LIMITED else None


def claude_transcript_limit(
    path: str | Path, tail_bytes: int = TRANSCRIPT_TAIL_BYTES
) -> LimitStop | None:
    """The last limit row at the end of a transcript, or None when there is none.

    Only the end of the file is read: the row belongs to the turn that has just
    finished, and a transcript grows without bound.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - tail_bytes)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return None
    lines = chunk.split(b"\n")
    if start > 0 and lines:
        # The first line of a mid-file read is half a row.
        lines = lines[1:]
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        limit = claude_row_limit(row)
        if limit is not None:
            return limit
    return None


def _names_usage_limit(info: Any) -> bool:
    """Whether a `CodexErrorInfo` names the account's usage limit.

    A unit variant arrives as the bare name, a variant that carries fields as
    an object keyed or tagged by it; all three shapes are read the same way.
    """
    if isinstance(info, str):
        return info.replace("_", "").lower() == CODEX_USAGE_LIMIT
    if isinstance(info, dict):
        names = [*info.keys(), info.get("type")]
        return any(_names_usage_limit(name) for name in names)
    return False


def codex_turn_limit(turn: dict[str, Any]) -> LimitStop | None:
    """The limit a Codex `turn/completed` reports, or None when it is another stop.

    The turn says only that the account's usage limit was reached: the failure
    carries `codexErrorInfo: "usageLimitExceeded"` and no time.
    `codex_limit_windows` is what puts the window and its reset on it.
    """
    if str(turn.get("status") or "") != "failed":
        return None
    error = turn.get("error")
    if not isinstance(error, dict):
        return None
    return LimitStop() if _names_usage_limit(error.get("codexErrorInfo")) else None


def codex_limit_windows(rate_limits: Any) -> LimitStop:
    """The window a Codex account ran out of, from `account/rateLimits/read` (A33).

    The read names both windows; the one the turn ran into is the one the
    account has used most of. A read that failed or named none leaves the stop
    with neither field, which is still a limit stop.
    """
    windows = parse_limits(rate_limits) if isinstance(rate_limits, dict) else []
    worst = max(windows, key=lambda window: window.used_percent, default=None)
    if worst is None:
        return LimitStop()
    return LimitStop(window_minutes=worst.window_minutes, resets_at_ms=worst.resets_at)
