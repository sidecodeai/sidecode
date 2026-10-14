"""Authentication and CSRF checks shared by HTTP routes and WebSocket upgrades.

Two credentials are accepted for the same identity: the HttpOnly cookie a browser gets at login, and
the identical token value in an ``Authorization: Bearer`` header for native apps. The distinction
matters for CSRF: a cookie travels automatically, so a cookie-authenticated mutating request (and
every cookie-authenticated upgrade) must also present an ``Origin`` equal to ``PUBLIC_ORIGIN``. A
bearer token is never attached by the browser on its own and needs no such check.
"""

from __future__ import annotations

import contextlib
import ipaddress
import time

from fastapi import HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

from .accounts import ROLE_ADMIN
from .auth import SESSION_COOKIE_NAME, SessionClaims, bearer_token, session_token_claims
from .frames import CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED
from .logging import logger
from .origins import origin_matches
from .rejects import RejectionLog
from .state import GatewayState

log = logger("rc_gateway.security")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Credential:
    """A verified session plus how it arrived, which decides whether Origin is checked."""

    def __init__(self, claims: SessionClaims, role: str, *, from_cookie: bool) -> None:
        self.claims = claims
        self.role = role
        self.from_cookie = from_cookie

    @property
    def username(self) -> str:
        return self.claims.username

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def state_of(request: Request | WebSocket) -> GatewayState:
    gateway: GatewayState = request.app.state.gateway
    return gateway


def _extract(request: Request | WebSocket) -> tuple[str, bool]:
    token = bearer_token(request.headers.get("authorization"))
    if token:
        return token, False
    return request.cookies.get(SESSION_COOKIE_NAME, ""), True


async def _verify(state: GatewayState, token: str, from_cookie: bool) -> Credential | None:
    """Authenticate a token and read the account behind it.

    The account is read on every request rather than trusted from the claims, so disabling or
    deleting one stops its requests immediately — and its live sockets close through the same
    revocation the routes trigger (A24).
    """
    if not token:
        return None
    claims = session_token_claims(token, state.config.secret)
    if claims is None or claims.expires_at <= time.time():
        return None
    if not await state.sessions.active(claims):
        return None
    account = await state.users.get(claims.username)
    if account is None or not account.active:
        return None
    return Credential(claims, account.role, from_cookie=from_cookie)


async def require_user(request: Request) -> Credential:
    """Authenticate an HTTP request and enforce the Origin rule for cookie mutations."""
    state = state_of(request)
    token, from_cookie = _extract(request)
    credential = await _verify(state, token, from_cookie)
    if credential is None:
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})
    if from_cookie and request.method not in SAFE_METHODS:
        require_origin(request)
    return credential


async def require_admin(request: Request) -> Credential:
    """Authenticate, then refuse everyone but the operator: the account routes of §3.9."""
    credential = await require_user(request)
    if not credential.is_admin:
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    return credential


def require_origin(request: Request) -> None:
    state = state_of(request)
    if not origin_matches(request.headers.get("origin"), state.config.public_origin):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})


def reject_foreign_origin(request: Request) -> None:
    """Enforce the Origin rule only on requests that carry the header.

    Used by ``POST /api/login``, which has no credential yet to key the cookie rule on. A browser
    always attaches ``Origin`` to a cross-site POST, so a mismatch is still refused and login CSRF
    stays blocked; a native app attaches none and must get through, because this endpoint is where
    it obtains the bearer token PROTOCOL.md section 2 tells it to use.
    """
    if "origin" in request.headers:
        require_origin(request)


async def authenticate_websocket(
    ws: WebSocket, rejects: RejectionLog | None = None
) -> Credential | None:
    """Accept the socket, then return its credential or close it with an A4 code.

    The accept comes first on purpose. Refusing the upgrade instead turns into an HTTP 403 at the
    handshake, and a WebSocket client never sees a close code for a handshake that never
    completed — so an app could not tell "your session expired, sign in again" from "the gateway
    is briefly unreachable, retry". Accepting and closing immediately, before a single frame is
    read, is what makes 4401 and 4403 observable.

    ``rejects`` is the flood valve ``/ws/device`` has had since a production gateway saw 685
    refused upgrades in three hours: an address far past any sane retry backoff is refused before
    the handshake instead, and its refusals are reported one line per window.
    """
    state = state_of(ws)
    token, from_cookie = _extract(ws)
    credential = await _verify(state, token, from_cookie)
    forbidden = (
        credential is not None
        and from_cookie
        and not origin_matches(ws.headers.get("origin"), state.config.public_origin)
    )
    if credential is not None and not forbidden:
        await ws.accept()
        return credential
    if rejects is not None and _report_refused(ws, state, rejects, forbidden):
        return None
    if not await accept_for_close(ws):
        return None
    if forbidden:
        await _close_unauthorized(ws, "origin not allowed", CLOSE_FORBIDDEN)
    else:
        await _close_unauthorized(ws)
    return None


def _report_refused(
    ws: WebSocket, state: GatewayState, rejects: RejectionLog, forbidden: bool
) -> bool:
    """Count one refused upgrade. True when the handshake should not be completed at all."""
    address = client_ip(ws, state)
    outcome = rejects.record(address)
    if outcome.report is not None:
        log.warning(
            "app upgrade rejected",
            reason="origin not allowed" if forbidden else "no valid credential",
            address=address,
            path=ws.scope.get("path", ""),
            attempts=outcome.report,
        )
    return outcome.flooding


async def accept_for_close(ws: WebSocket) -> bool:
    """Complete the handshake so a close code can be delivered. False if that is impossible."""
    try:
        await ws.accept()
    except (RuntimeError, WebSocketDisconnect):
        # The handshake itself failed; the caller gets the transport-level rejection instead.
        return False
    return True


async def _close_unauthorized(
    ws: WebSocket, reason: str = "unauthorized", code: int = CLOSE_UNAUTHORIZED
) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await ws.close(code=code, reason=reason)


def client_ip(request: Request | WebSocket, state: GatewayState) -> str:
    """The caller's address, trusting ``X-Forwarded-For`` only from a configured proxy.

    Without this rule every user behind the proxy would share one rate-limit bucket. With a naive
    rule the opposite breaks: a proxy that *appends* to the header (the standard nginx recipe
    does) leaves a caller-supplied value in front, so reading the first element would let anyone
    choose their own bucket and escape the login limit. The address taken is therefore the last
    hop that is not itself a trusted proxy, walking the chain from the right.
    """
    peer = request.client.host if request.client else ""
    if not peer or not _is_trusted_proxy(peer, state):
        return peer or "unknown"
    hops = [item.strip() for item in request.headers.get("x-forwarded-for", "").split(",")]
    for hop in reversed([item for item in hops if item]):
        if not _is_trusted_proxy(hop, state):
            return hop
    return peer


def _is_trusted_proxy(peer: str, state: GatewayState) -> bool:
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    for network in state.config.trusted_proxy_networks:
        try:
            if address in ipaddress.ip_network(network, strict=False):
                return True
        except (ValueError, TypeError):
            continue
    return False
