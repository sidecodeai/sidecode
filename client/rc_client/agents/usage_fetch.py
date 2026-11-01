"""One GET of Anthropic's OAuth usage endpoint, and what came of it (A33).

The read is dialled the way this device was enrolled to dial (`proxy.httpx_options`), has one
deadline in all, and says one line about itself in the log: the status, the vendor's own error
type, the wait it asked for, the content type and how long it took. The token, the headers and the
body never reach that line, or any other.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..logging_setup import logger
from ..models import now_ms
from ..proxy import httpx_options

log = logger("rc_client.anthropic_usage")

HOST = "api.anthropic.com"
USAGE_URL = f"https://{HOST}/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
# One deadline for the whole read. httpx's own limits restart for connecting, writing, reading and
# waiting for a pooled connection, so on their own they let a read through a proxy take several
# times as long.
DEADLINE = 5.0
# The longest `Retry-After` that is taken at its word.
MAX_WAIT = 86_400
SLOW = f"{HOST} did not answer in time"
FORBIDDEN = "this sign-in is not allowed to read the quota (HTTP 403)"
LIMITING = f"{HOST} is limiting quota reads (HTTP 429)"
UNREADABLE = f"{HOST} returned a usage body this device cannot read"
# The vendor's error types are short snake_case words; anything else is not logged.
_ERROR_TYPE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_SECONDS = re.compile(r"[0-9]{1,9}")


@dataclass(frozen=True, slots=True)
class Reading:
    """What one read of the usage endpoint came to: its body, or the one line that says why not.

    `transient` marks a failure likely to pass by itself: the vendor limiting reads or refusing for
    now (429, 403), a server error, a timeout, no connection. The windows read before may stand in
    for the ones it could not bring. A 401, which only the person can mend, a body that cannot be
    used and a status the device does not know are not that. `wait` is the seconds a `Retry-After`
    asked for, and `at` the protocol time the read ended.
    """

    body: dict[str, Any] | None = None
    error: str = ""
    status: int | None = None
    wait: int | None = None
    transient: bool = False
    at: int = 0


async def fetch(token: str, proxy: str = "") -> Reading:
    """The usage body the token buys, or the one line that says why there is none.

    It never raises: whatever goes wrong, in dialling or in reading what came back, is the line.
    """
    started = time.monotonic()
    seen: dict[str, str | None]
    try:
        response = await _get(token, proxy)
        reading, seen = _read(response)
    except Exception as exc:  # a quota read must never cost the account itself
        reading, seen = _failed(exc), {"failure": type(exc).__name__}
    _log(reading, started, **seen)
    return reading


def _read(response: httpx.Response) -> tuple[Reading, dict[str, str | None]]:
    """What an answer comes to, and the parts of it that the log may say."""
    body = _json(response)
    seen = {"content_type": _media_type(response), "error_type": _error_type(body)}
    return _answered(response, body), seen


async def _get(token: str, proxy: str) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
    }
    # Dialled as every other call of this device is: through the configured proxy, never by the
    # environment (proxy.py). The host is always Anthropic's own: OAuth usage is never relayed.
    async with (
        asyncio.timeout(DEADLINE),
        httpx.AsyncClient(timeout=DEADLINE, **httpx_options(proxy)) as client,
    ):
        return await client.get(USAGE_URL, headers=headers)


def _failed(exc: Exception) -> Reading:
    at = now_ms()
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return Reading(error=SLOW, transient=True, at=at)
    if isinstance(exc, httpx.HTTPError):
        return Reading(error=f"could not reach {HOST}: {type(exc).__name__}", transient=True, at=at)
    return Reading(error=f"could not read the Anthropic quota: {type(exc).__name__}", at=at)


def _answered(response: httpx.Response, body: dict[str, Any] | None) -> Reading:
    status = response.status_code
    at = now_ms()
    if status == 200:
        if body is None:
            return Reading(error=UNREADABLE, status=status, at=at)
        return Reading(body=body, status=status, at=at)
    wait = _retry_after(response.headers.get("retry-after"))
    return Reading(
        error=_refusal(status, wait),
        status=status,
        wait=wait,
        transient=status in (403, 429) or status >= 500,
        at=at,
    )


def _refusal(status: int, wait: int | None) -> str:
    """One line for a status other than 200; a 401 is worded by whoever holds the token."""
    if status == 403:
        return FORBIDDEN
    if status == 429:
        return f"{LIMITING}; try again in {_span(wait)}" if wait else f"{LIMITING}; try again later"
    if status >= 500:
        return f"{HOST} had a server error (HTTP {status})"
    return f"{HOST} answered HTTP {status}"


def _span(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{-(-seconds // 60)} min"
    return f"{-(-seconds // 3600)} h"


def _retry_after(value: str | None) -> int | None:
    """The seconds a `Retry-After` asks for, given as a number or as the date it names.

    The header is the vendor's, or a proxy's, text: whatever is wrong with it, it names no wait.
    """
    text = (value or "").strip()
    if _SECONDS.fullmatch(text):
        return min(int(text), MAX_WAIT)
    try:
        date = parsedate_to_datetime(text)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        wait = int((date - datetime.now(UTC)).total_seconds())
    except Exception:
        return None
    return max(0, min(wait, MAX_WAIT))


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, dict) else None


def _error_type(body: dict[str, Any] | None) -> str | None:
    """The vendor's own name for what went wrong, when its body names one in the usual place."""
    error = body.get("error") if body else None
    kind = error.get("type") if isinstance(error, dict) else None
    return kind if isinstance(kind, str) and _ERROR_TYPE.fullmatch(kind) else None


def _media_type(response: httpx.Response) -> str | None:
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return kind[:64] or None


def _log(reading: Reading, started: float, **seen: str | None) -> None:
    """The one line a read leaves: only what is known about the answer, never what it said."""
    fields: dict[str, Any] = {
        "status": reading.status,
        "retry_after": reading.wait,
        "duration_ms": round((time.monotonic() - started) * 1000),
        **seen,
    }
    log.info(
        "anthropic usage read", **{key: value for key, value in fields.items() if value is not None}
    )
