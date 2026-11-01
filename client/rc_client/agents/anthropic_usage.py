"""What is left of an Anthropic account's quota, read with its own token (A33).

Claude Code and pi can both be signed in to Anthropic, and both keep an access
token the vendor's usage endpoint accepts, so the read lives here once. The
token buys one GET and nothing else: it is never stored, never refreshed and
never written to a log or on to the wire — only the percentages come back out.

`usage_fetch` makes the request and `usage_memory` holds what the device keeps
between two of them. This module turns what came back into an account's windows,
or into the one line that says why there are none.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..logging_setup import logger
from ..models import AgentAccount, AgentLimit, now_ms
from .usage_fetch import HOST, Reading, fetch
from .usage_memory import UsageMemory

log = logger("rc_client.anthropic_usage")

SESSION_MINUTES = 300
WEEKLY_MINUTES = 10080
# The window each `limits[]` row stands for; a kind this device does not know
# is skipped rather than guessed at.
WINDOWS = {
    "session": SESSION_MINUTES,
    "weekly_all": WEEKLY_MINUTES,
    "weekly_scoped": WEEKLY_MINUTES,
}
EXPIRED = "signed-in token expired; sign in again to read the quota"
REJECTED = "signed-in token rejected (HTTP 401); sign in again to read the quota"
NO_TOKEN = "no signed-in token to read the quota with"
NO_WINDOWS = f"{HOST} reported no quota windows this device recognises"

# What the device keeps between reads, for the life of the process.
memory = UsageMemory()


@dataclass(slots=True)
class Bearer:
    """An Anthropic access token, when it stops working, and who can renew it.

    `expired_message` is what the person is told once `expires_at` has passed
    and `rejected_message` what they are told when the vendor refuses the token
    before that: only they can refresh it, by opening the agent that owns it.
    """

    token: str
    expires_at: int | None = None
    expired_message: str = EXPIRED
    rejected_message: str = REJECTED


# Reads the agent's credential again, for a token that may have just been rotated.
Renew = Callable[[], Awaitable[Bearer | None]]


async def fill_limits(
    agent: str,
    account: AgentAccount,
    bearer: Bearer | None,
    proxy: str = "",
    renew: Renew | None = None,
) -> None:
    """Read `account`'s windows, or say in one line why they are missing.

    `agent` is the one whose credential it is. A token the vendor refuses may have been rotated
    since it was read, so `renew` is asked for the credential once and the read is repeated once
    with a token that differs. When the vendor cannot answer right now (it is limiting reads, has a
    server error, is slow or unreachable) the windows read last stand in if they are recent
    (`fall_back`); a failure only the person can mend never lets them.
    """
    account.limits_checked_at = now_ms()
    if bearer is None or not bearer.token:
        account.limits_error = NO_TOKEN
        return
    if _expired(bearer):
        account.limits_error = bearer.expired_message
        return
    reading = await _read(bearer.token, proxy)
    if reading.status == 401 and renew is not None:
        fresh = await renew()
        if fresh is not None and fresh.token not in ("", bearer.token) and not _expired(fresh):
            bearer = fresh
            reading = await _read(fresh.token, proxy)
    _apply(agent, account, bearer, reading)


def fall_back(agent: str, account: AgentAccount, reason: str) -> None:
    """Fill `account` from the windows read last when they are recent, else say why there are none.

    A recent earlier reading answers without a `limits_error`: it carries the time it was read at,
    and an app that draws the line draws no meter. The reason goes to the log instead, for whoever
    asks why the numbers are not new.
    """
    now = now_ms()
    who = _who(agent, account)
    kept = memory.recall(who, now) if who is not None else None
    if kept is None:
        account.limits_checked_at = now
        account.limits_error = reason
        return
    account.limits = list(kept.limits)
    account.limits_checked_at = kept.checked_at
    log.info(
        "anthropic usage served from an earlier read",
        reason=reason,
        age_s=(now - kept.checked_at) // 1000,
        windows=len(kept.limits),
    )


def parse(body: dict[str, Any]) -> list[AgentLimit]:
    """The windows a usage body reports: its `limits` rows, else the older pair."""
    rows = body.get("limits")
    found = (
        [limit for row in rows if (limit := _row(row)) is not None]
        if isinstance(rows, list)
        else []
    )
    return found or _legacy(body)


async def _read(token: str, proxy: str) -> Reading:
    return await memory.read(token, lambda: fetch(token, proxy))


def _apply(agent: str, account: AgentAccount, bearer: Bearer, reading: Reading) -> None:
    if reading.body is None:
        if reading.status == 401:
            account.limits_error = bearer.rejected_message
        elif reading.transient:
            fall_back(agent, account, reading.error)
        else:
            account.limits_error = reading.error
        return
    limits = parse(reading.body)
    if not limits:
        account.limits_error = NO_WINDOWS
        return
    who = _who(agent, account)
    if who is not None:
        memory.remember(who, limits, reading.at)
    account.limits = limits
    account.limits_checked_at = reading.at


def _who(agent: str, account: AgentAccount) -> str | None:
    """Whose windows they are, so that no other account ever inherits them.

    Nothing but the email tells two accounts of one vendor apart, so an account with none is
    nobody's: its windows are neither kept nor stood in for.
    """
    return f"{agent}:{account.provider}:{account.email}" if account.email else None


def _expired(bearer: Bearer) -> bool:
    return bearer.expires_at is not None and bearer.expires_at <= now_ms()


def _row(row: Any) -> AgentLimit | None:
    if not isinstance(row, dict):
        return None
    minutes = WINDOWS.get(str(row.get("kind")))
    percent = row.get("percent")
    if minutes is None or isinstance(percent, bool) or not isinstance(percent, int | float):
        return None
    return _limit(minutes, percent, _moment(row.get("resets_at")), _scope(row.get("scope")))


def _limit(
    minutes: int, percent: float, resets_at: int | None, scope: str | None = None
) -> AgentLimit | None:
    try:
        return AgentLimit(
            window_minutes=minutes, used_percent=percent, resets_at=resets_at, scope=scope
        )
    except (ArithmeticError, ValueError):  # a percent too large to be a float
        return None


def _scope(scope: Any) -> str | None:
    """A scoped weekly window names the model it is confined to."""
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    return name if isinstance(name, str) and name else None


def _legacy(body: dict[str, Any]) -> list[AgentLimit]:
    """The older `five_hour` / `seven_day` pair, for a body with no `limits`."""
    limits: list[AgentLimit] = []
    for key, minutes in (("five_hour", SESSION_MINUTES), ("seven_day", WEEKLY_MINUTES)):
        window = body.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("utilization")
        if isinstance(used, bool) or not isinstance(used, int | float):
            continue
        if limit := _limit(minutes, used, _moment(window.get("resets_at"))):
            limits.append(limit)
    return limits


def _moment(value: Any) -> int | None:
    """An ISO 8601 instant as protocol milliseconds; anything else as nothing."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)
