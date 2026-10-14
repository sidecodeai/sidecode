"""Security headers on every HTTP response.

A TLS proxy used to add these in front of the gateway. The compose stack no longer ships one — the
operator points their own reverse proxy at the published port — so the gateway sets them itself and
a deployment never depends on what that proxy happens to send. Headers a route already set win, and
WebSocket handshakes are left untouched.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob:; "
    "worker-src 'self' blob:; connect-src 'self' wss:; frame-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
#: The composer records voice, so the microphone has to stay allowed for this origin.
PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=(), payment=(), usb=()"
STRICT_TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"

Header = tuple[bytes, bytes]
BASE_HEADERS: tuple[Header, ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", PERMISSIONS_POLICY.encode("ascii")),
)
HSTS_HEADER: Header = (
    b"strict-transport-security",
    STRICT_TRANSPORT_SECURITY.encode("ascii"),
)


class SecurityHeaders:
    """Add the fixed response headers, plus HSTS when the public origin is https."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.headers = BASE_HEADERS + ((HSTS_HEADER,) if hsts else ())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, _decorated(send, self.headers))


def _decorated(send: Send, headers: tuple[Header, ...]) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = _merged(message.get("headers", ()), headers)
        await send(message)

    return wrapped


def _merged(existing: Iterable[Header], extra: tuple[Header, ...]) -> list[Header]:
    """Append the headers the response does not already carry, matching case-insensitively."""
    present = list(existing)
    taken = {name.lower() for name, _ in present}
    return present + [(name, value) for name, value in extra if name not in taken]
