"""The caller's own preferences (PROTOCOL 3.2, A35, A41).

Only the caller's account is readable or writable: there is no path here that names a username, so
one person's preferences cannot be read or changed by another. A change is published the moment it
is stored — ``preferences.updated`` to the account's app sockets, ``preferences`` to its devices —
so a device acts on the new value without being asked and a second app never shows a stale one.
The gateway is the single writer, and the order the writes arrive in is the order of truth.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..logging import logger
from ..preference_store import FIELDS, Preferences
from ..security import Credential, require_user, state_of
from .session_routes import _bounded_body

log = logger("rc_gateway.preferences")
router = APIRouter()

#: A few booleans and short words. Far above anything the wire defines — the longest field the
#: schema allows is a 128-character model name — and far below a payload worth spooling.
BODY_MAX_BYTES = 4096


@router.get("/api/preferences")
async def read_preferences(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    return _response(await state.preference_store.get(credential.username))


@router.patch("/api/preferences")
async def patch_preferences(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    body = await _bounded_body(request, BODY_MAX_BYTES)
    values = _changes(body)
    before = await state.preference_store.get(credential.username)
    after = await state.preference_store.patch(credential.username, values)
    if after != before:
        log.info("preferences changed", username=credential.username)
        await state.publish_preferences(credential.username, after)
    return _response(after)


def _boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _word(*allowed: str) -> Callable[[Any], bool]:
    """One of the words the field's enum lists, and nothing else."""
    return lambda value: isinstance(value, str) and value in allowed


def _string(*, minimum: int, maximum: int) -> Callable[[Any], bool]:
    """A string the schema bounds. ``minimum`` is 0 where the empty string means "none"."""
    return lambda value: isinstance(value, str) and minimum <= len(value) <= maximum


#: What each field of ``objects.json#/$defs/Preferences`` accepts. The gateway is the one writer,
#: so a value that fails its check is refused here rather than published to every app and device.
CHECKS: dict[str, Callable[[Any], bool]] = {
    "resume_after_limit": _boolean,
    "language": _word("en", "zh-Hans"),
    "stt_language": _string(minimum=1, maximum=32),
    "polish_enabled": _boolean,
    "polish_model": _string(minimum=0, maximum=128),
    "polish_strength": _word("moderate", "strong"),
    "timeline_detail": _word("simple", "detailed"),
}


def _changes(body: dict[str, Any]) -> dict[str, Any]:
    """The preferences this body sets. Absent means unchanged; a wrong value is a bad request.

    A field the gateway does not know is ignored rather than refused, as the protocol's preamble
    requires of every component reading a frame from a newer peer.
    """
    values: dict[str, Any] = {}
    for name in FIELDS:
        if name not in body:
            continue
        value = body[name]
        if not CHECKS[name](value):
            raise HTTPException(status_code=400, detail={"code": "bad_request"})
        values[name] = value
    return values


def _response(preferences: Preferences) -> JSONResponse:
    return JSONResponse({"preferences": preferences.view()}, headers={"Cache-Control": "no-store"})
