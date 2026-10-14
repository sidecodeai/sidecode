"""Pair this host by having someone scan a QR code (A23).

The host asks the gateway for a claim token and waits on it; an app scans the
token's URL and claims it, the gateway mints the ordinary pairing code for that
account and hands it back here, and enrolment proceeds exactly as it does for a
code typed by hand.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from .errors import RcError

REQUESTS_PATH = "/api/pairing/requests"
# The gateway holds a poll for 25 s, so the request outlives it comfortably.
POLL_TIMEOUT = 40.0
# The claim token lives ten minutes; there is nothing to wait for after that.
TOTAL_WAIT = 600.0
POLL_PAUSE = 1.0

EXPIRED = "the code expired; run this again"
FORGOTTEN = "the gateway has forgotten this request; run this again"
GAVE_UP = "nobody scanned the code; run this again"


@dataclass(slots=True)
class ClaimRequest:
    token: str
    claim_url: str
    expires_at: int


def _body(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RcError("internal", "the gateway returned a malformed pairing response") from exc
    if not isinstance(payload, dict):
        raise RcError("internal", "the gateway returned a malformed pairing response")
    return payload


async def create_request(origin: str, client: httpx.AsyncClient) -> ClaimRequest:
    """Ask the gateway for a claim token to print."""
    try:
        response = await client.post(f"{origin}{REQUESTS_PATH}")
    except httpx.HTTPError as exc:
        raise RcError("internal", f"cannot reach {origin}: {type(exc).__name__}") from exc
    if response.status_code == 429:
        raise RcError("conflict", "the gateway is rate limiting pairing requests; wait a minute")
    if response.status_code >= 400:
        raise RcError("internal", f"the gateway answered HTTP {response.status_code}")
    payload = _body(response)
    token = str(payload.get("token") or "")
    claim_url = str(payload.get("claim_url") or "")
    if not token or not claim_url:
        raise RcError("internal", "the gateway did not return a claim token")
    expires = payload.get("expires_at")
    return ClaimRequest(
        token=token,
        claim_url=claim_url,
        expires_at=int(expires) if isinstance(expires, int) else 0,
    )


async def await_code(
    origin: str,
    token: str,
    client: httpx.AsyncClient,
    *,
    pause: float = POLL_PAUSE,
    total_wait: float = TOTAL_WAIT,
) -> str:
    """Long-poll until an app claims the token, and return the pairing code."""
    deadline = time.monotonic() + total_wait
    while time.monotonic() < deadline:
        try:
            response = await client.get(f"{origin}{REQUESTS_PATH}/{token}")
        except httpx.TimeoutException:
            continue
        except httpx.HTTPError as exc:
            raise RcError("internal", f"cannot reach {origin}: {type(exc).__name__}") from exc
        if response.status_code == 410:
            raise RcError("not_found", EXPIRED)
        if response.status_code == 404:
            raise RcError("not_found", FORGOTTEN)
        if response.status_code >= 400:
            raise RcError("internal", f"the gateway answered HTTP {response.status_code}")
        payload = _body(response)
        if payload.get("status") == "claimed":
            code = str(payload.get("code") or "")
            if not code:
                raise RcError("internal", "the gateway claimed the request without a code")
            return code
        await asyncio.sleep(pause)
    raise RcError("timeout", GAVE_UP)
