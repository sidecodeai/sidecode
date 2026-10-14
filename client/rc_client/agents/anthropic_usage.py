"""What is left of an Anthropic account's quota, read with its own token (A33).

Claude Code and pi can both be signed in to Anthropic, and both keep an access
token the vendor's usage endpoint accepts, so the read lives here once. The
token buys one GET and nothing else: it is never stored, never refreshed and
never written to a log or on to the wire — only the percentages come back out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ..models import AgentAccount, AgentLimit, now_ms

HOST = "api.anthropic.com"
USAGE_URL = f"https://{HOST}/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
TIMEOUT = 5.0

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
NO_TOKEN = "no signed-in token to read the quota with"


@dataclass(slots=True)
class Bearer:
    """An Anthropic access token, when it stops working, and who can renew it.

    `expired_message` is what the person is told once `expires_at` has passed:
    only they can refresh the token, by opening the agent that owns it.
    """

    token: str
    expires_at: int | None = None
    expired_message: str = EXPIRED


async def fill_limits(account: AgentAccount, bearer: Bearer | None) -> None:
    """Read `account`'s windows, or say in one line why they are missing."""
    account.limits_checked_at = now_ms()
    if bearer is None or not bearer.token:
        account.limits_error = NO_TOKEN
        return
    if bearer.expires_at is not None and bearer.expires_at <= now_ms():
        account.limits_error = bearer.expired_message
        return
    body, error = await fetch(bearer.token)
    if body is None:
        account.limits_error = error
        return
    account.limits = parse(body)


def parse(body: dict[str, Any]) -> list[AgentLimit]:
    """The windows a usage body reports, preferring its `limits` rows."""
    rows = body.get("limits")
    if isinstance(rows, list) and rows:
        return [limit for row in rows if (limit := _row(row)) is not None]
    return _legacy(body)


async def fetch(token: str) -> tuple[dict[str, Any] | None, str]:
    """The usage body the token buys, or the one line that says why there is none."""
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
    }
    try:
        # Dialled directly, like every other call this device makes: OAuth usage
        # is never relayed, so the host is always Anthropic's own.
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False) as client:
            response = await client.get(USAGE_URL, headers=headers)
    except httpx.HTTPError as exc:
        return None, f"could not reach {HOST}: {type(exc).__name__}"
    if response.status_code != 200:
        return None, f"{HOST} answered HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, f"{HOST} returned a usage body this device cannot read"
    if not isinstance(body, dict):
        return None, f"{HOST} returned a usage body this device cannot read"
    return body, ""


def _row(row: Any) -> AgentLimit | None:
    if not isinstance(row, dict):
        return None
    minutes = WINDOWS.get(str(row.get("kind")))
    percent = row.get("percent")
    if minutes is None or isinstance(percent, bool) or not isinstance(percent, int | float):
        return None
    return AgentLimit(
        window_minutes=minutes,
        used_percent=percent,
        resets_at=_moment(row.get("resets_at")),
        scope=_scope(row.get("scope")),
    )


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
        limits.append(
            AgentLimit(
                window_minutes=minutes,
                used_percent=used,
                resets_at=_moment(window.get("resets_at")),
            )
        )
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
