"""What is left of the Grok Build account's quota, as its leader reads it (A53).

`/usage` in the TUI reads it through the extension method `_x.ai/billing`,
which the agent answers by asking xAI under the session it is signed in with.
The device asks the leader it holds a connection to, so it never sends a token
or names an endpoint itself, and the leader keeps its own session fresh. Only
leader mode has a leader to ask: with it off nothing is asked and no window is
reported, since a private child would start a whole agent for one read, with
the requests and writes every start of Grok makes. With it on the quota always
shows, and while the device is still joining the leader the read says so.

The reply's `config` names the current usage period, a week or a month, and
the share of it used. xAI leaves `creditUsagePercent` out while none of the
period is used, as proto3 JSON leaves out every zero, and Grok's TUI shows such
a reply as 0%.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from ...errors import RcError
from ...models import AgentAccount, AgentLimit, now_ms
from ..registry import BillingReader
from .acp import GrokError

BILLING = "_x.ai/billing"
LIMITS_TIMEOUT = 5.0
# The periods a window is drawn for; any other kind reports none.
PERIODS = frozenset({"USAGE_PERIOD_TYPE_WEEKLY", "USAGE_PERIOD_TYPE_MONTHLY"})
SLOW_LEADER = "the Grok leader did not answer in time"
NOT_JOINED = "not connected to the Grok leader yet; the device keeps trying"
NO_REASON = "Grok gave no reason"
MAX_ERROR = 200
# An address in Grok's words is cut down to its host, as an endpoint always is
# on the wire (A33): a configured base URL is where a secret could ride along,
# and a bare `host:port` loses its port the same way.
_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^\s)\"'<>]+", re.IGNORECASE)
_HOST_PORT = re.compile(
    r"\b(localhost|(?:[a-z0-9-]+\.)+[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}\b", re.IGNORECASE
)


class LeaderModeOff(RcError):
    """Grok's configuration does not ask for the leader, so there is none to ask."""

    def __init__(self) -> None:
        super().__init__("agent_unavailable", "grok leader mode is off")


class LeaderNotJoined(RcError):
    """Leader mode is on, and the device has not joined the leader, or has lost it."""

    def __init__(self) -> None:
        super().__init__("agent_unavailable", NOT_JOINED)


async def fill_limits(account: AgentAccount, billing: BillingReader) -> None:
    """Read `account`'s window from the leader, or say in one line why it is missing.

    With leader mode off there is nothing to read and nothing is said.
    """
    checked_at = now_ms()
    try:
        reply = await asyncio.wait_for(billing(), LIMITS_TIMEOUT)
    except LeaderModeOff:
        return
    except LeaderNotJoined:
        account.limits_error = NOT_JOINED
    except TimeoutError:
        account.limits_error = SLOW_LEADER
    except GrokError as exc:
        account.limits_error = reason(exc.detail or exc.message)
    except RcError as exc:
        account.limits_error = reason(exc.message)
    except Exception as exc:  # a quota read must never cost the account itself
        account.limits_error = f"could not read the Grok quota: {type(exc).__name__}"
    else:
        account.limits = parse_limits(reply)
    account.limits_checked_at = checked_at


def parse_limits(reply: dict[str, Any]) -> list[AgentLimit]:
    """The current period as one window, or none when xAI names no period."""
    config = reply.get("config")
    if not isinstance(config, dict):
        return []
    period = config.get("currentPeriod")
    if not isinstance(period, dict) or period.get("type") not in PERIODS:
        return []
    start, end = _instant(period.get("start")), _instant(period.get("end"))
    if start is None or end is None:
        return []
    minutes = round((end - start).total_seconds() / 60)
    used = config.get("creditUsagePercent")
    if used is None:
        used = 0
    if minutes < 1 or isinstance(used, bool) or not isinstance(used, int | float):
        return []
    return [
        AgentLimit(
            window_minutes=minutes,
            used_percent=used,
            resets_at=int(end.timestamp() * 1000),
        )
    ]


def reason(message: str) -> str:
    """Grok's own words for a failed read: its first line, every address a host."""
    lines = message.strip().splitlines()
    line = lines[0].strip() if lines else ""
    if not line:
        return NO_REASON
    return _HOST_PORT.sub(r"\1", _URL.sub(_host, line))[:MAX_ERROR]


def _host(match: re.Match[str]) -> str:
    try:
        host = urlsplit(match.group(0)).hostname
    except ValueError:
        host = None
    return host or "an address"


def _instant(value: Any) -> datetime | None:
    """An RFC 3339 time; one without an offset is taken to be UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
