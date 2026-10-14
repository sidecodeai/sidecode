"""Dictation polish: the provider's models, and one dictation cleaned up (A29).

Both paths are authenticated exactly like `/api/stt/transcribe` and answer `503` `unsupported`
while no polish model is configured. Nothing a caller sends is stored or logged: the body is
validated, passed to the provider and forgotten, and only the polished text goes back.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..logging import logger
from ..polish import Polisher, PolishError
from ..polish_prompt import (
    CONTEXT_ROLES,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_ITEMS,
    MAX_TEXT_CHARS,
    STRENGTHS,
    ContextMessage,
)
from ..security import Credential, client_ip, require_user, state_of
from ..state import GatewayState
from .session_routes import _bounded_body

log = logger("rc_gateway.polish")
router = APIRouter()

#: An 8192-character text plus twenty 4000-character turns, with room for multi-byte characters.
BODY_MAX_BYTES = 512 * 1024
MAX_MODEL_CHARS = 200
MAX_LANGUAGE_CHARS = 32


@router.get("/api/polish/models")
async def polish_models(request: Request, _: Credential = Depends(require_user)) -> JSONResponse:
    state = state_of(request)
    polisher = _polisher(state)
    _rate_limit(request, state)
    try:
        models = await polisher.models()
    except PolishError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code}) from exc
    return JSONResponse(
        {"models": [{"id": item.id, "label": item.label} for item in models]},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/polish")
async def polish(request: Request, _: Credential = Depends(require_user)) -> JSONResponse:
    state = state_of(request)
    polisher = _polisher(state)
    _rate_limit(request, state)
    body = await _bounded_body(request, BODY_MAX_BYTES)
    text = _text(body.get("text"))
    model = _model(state, body.get("model"))
    strength = _strength(body.get("strength"))
    language = _language(body.get("language"))
    context = _context(body.get("context"))
    try:
        polished = await polisher.polish(
            text, model=model, strength=strength, language=language, context=context
        )
    except PolishError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code}) from exc
    # Length and turn count only: the text itself is the user's speech and never reaches the log.
    log.info("dictation polished", strength=strength, context_messages=len(context))
    return JSONResponse({"text": polished}, headers={"Cache-Control": "no-store"})


def _polisher(state: GatewayState) -> Polisher:
    if state.polisher is None:
        raise HTTPException(status_code=503, detail={"code": "unsupported"})
    return state.polisher


def _rate_limit(request: Request, state: GatewayState) -> None:
    """One operator key pays for every call, so a burst from one address is refused."""
    if state.polish_limiter.limited(client_ip(request, state)):
        log.warning("polish rate limited")
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})


def _bad_request() -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "bad_request"})


def _text(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS or not value.strip():
        raise _bad_request()
    return value


def _model(state: GatewayState, value: Any) -> str:
    """The model the user chose, checked against the allowlist when the operator set one.

    Without this the value would reach a third-party API as-is, and an allowlisted gateway would
    still bill the operator for a model they never offered.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_MODEL_CHARS:
        raise _bad_request()
    allowed = state.config.polish.models
    if allowed and value not in allowed:
        raise _bad_request()
    return value


def _strength(value: Any) -> str:
    if value not in STRENGTHS:
        raise _bad_request()
    return str(value)


def _language(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_LANGUAGE_CHARS:
        raise _bad_request()
    return value


def _context(value: Any) -> list[ContextMessage]:
    if not isinstance(value, list) or len(value) > MAX_CONTEXT_ITEMS:
        raise _bad_request()
    messages: list[ContextMessage] = []
    for item in value:
        if not isinstance(item, dict):
            raise _bad_request()
        role, text = item.get("role"), item.get("text")
        if role not in CONTEXT_ROLES:
            raise _bad_request()
        if not isinstance(text, str) or len(text) > MAX_CONTEXT_CHARS:
            raise _bad_request()
        messages.append(ContextMessage(role=str(role), text=text))
    return messages
