"""Authenticate and bound uploads before their body is read.

FastAPI parses a multipart form *before* it solves the route's dependencies, so a
``Depends(require_user)`` on the handler only runs once the whole upload has been spooled — to disk,
past 1 MiB, with no ceiling. An anonymous caller could therefore fill the gateway's writable layer
with concurrent slow uploads. This middleware runs ahead of routing instead: it refuses an
unauthenticated caller, rejects an over-large ``Content-Length`` outright, and counts the bytes of a
chunked body as they stream so the cap holds without a declared length.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from fastapi import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logging import logger
from .security import require_user

log = logger("rc_gateway.uploads")

Verifier = Callable[[Request], Awaitable[Any]]


class BoundedUploads:
    """Guard the upload routes named in ``paths``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: Iterable[str],
        max_bytes: int,
        verify: Verifier = require_user,
    ) -> None:
        self.app = app
        self.paths = frozenset(paths)
        self.max_bytes = max_bytes
        self.verify = verify

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        declared = _content_length(request)
        if declared is not None and declared > self.max_bytes:
            await _error(send, 413, "too_large", "upload is too large")
            return
        try:
            await self.verify(request)
        except HTTPException as exc:
            detail: Any = exc.detail
            code = str(detail.get("code")) if isinstance(detail, dict) else "unauthorized"
            await _error(send, exc.status_code, code, code.replace("_", " "))
            return

        await self.app(scope, _counted(receive, self.max_bytes), send)


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _counted(receive: Receive, max_bytes: int) -> Receive:
    """Stop a chunked body at ``max_bytes`` by ending the stream early.

    The route then sees a truncated form and answers ``bad_request`` or ``too_large`` itself; what
    matters here is that nothing beyond the cap is ever buffered or spooled.
    """
    seen = 0

    async def wrapped() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] != "http.request":
            return message
        body = message.get("body", b"")
        seen += len(body)
        if seen > max_bytes:
            log.warning("upload exceeded the cap", bytes=seen)
            return {"type": "http.request", "body": b"", "more_body": False}
        return message

    return wrapped


async def _error(send: Send, status: int, code: str, message: str) -> None:
    body = (
        b'{"ok":false,"error":{"code":"'
        + code.encode("ascii", "ignore")
        + b'","message":"'
        + message.encode("ascii", "ignore")
        + b'"}}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
