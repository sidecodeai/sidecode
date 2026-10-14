"""Login, logout, session introspection, health and client configuration."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..auth import (
    LOGIN_FORBIDDEN,
    SESSION_COOKIE_NAME,
    authenticate_login,
    make_session_token,
    session_token_claims,
)
from ..config import SESSION_TTL_SECONDS
from ..frames import PROTOCOL_VERSION
from ..logging import logger
from ..security import Credential, client_ip, reject_foreign_origin, require_user, state_of
from ..state import VERSION, GatewayState
from ..users import UserRecord
from ..views import user_view

log = logger("rc_gateway.auth")
router = APIRouter()

LOGIN_BODY_MAX_BYTES = 4096
_LOGIN_STATUS = {LOGIN_FORBIDDEN: 403}


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    state = state_of(request)
    return JSONResponse(
        {
            "ok": True,
            "version": VERSION,
            "protocol": PROTOCOL_VERSION,
            # A31, A45. The one unauthenticated endpoint, so an iPhone or Mac app too old for this
            # gateway learns it before anyone signs in rather than after a credential it cannot use.
            "apps": state.apps_view(),
            "auth": {
                "mode": "password",
                # A24. The only unauthenticated way an app learns whether to offer
                # "Create an account", so the login page reads it before anyone has signed in.
                "registration_open": await state.users.registration_open(),
            },
            "devices_online": len(state.hub.online_device_ids()),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/login")
async def login(request: Request) -> Response:
    state = state_of(request)
    reject_foreign_origin(request)
    address = client_ip(request, state)
    if state.login_limiter.limited(address):
        log.warning("login rate limited")
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})

    body = await _bounded_body(request, LOGIN_BODY_MAX_BYTES)
    username, password = body.get("username"), body.get("password")
    # A24: the account is part of the credential, so a body without one is a bad request rather
    # than a login as the operator.
    if not isinstance(username, str) or not username or not isinstance(password, str):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    resolved = await authenticate_login(state.users, username, password, state.config.password)
    if isinstance(resolved, str):
        raise HTTPException(status_code=_LOGIN_STATUS.get(resolved, 401), detail={"code": resolved})
    await state.users.touch_login(resolved.username)
    log.info("login succeeded", username=resolved.username)
    return await issue_session(state, resolved)


async def issue_session(state: GatewayState, account: UserRecord) -> Response:
    """Mint a token for an account and set the cookie: what login and registration both end in."""
    token, expires_at = make_session_token(
        state.config.secret, SESSION_TTL_SECONDS, username=account.username
    )
    claims = session_token_claims(token, state.config.secret)
    if claims is None:
        raise HTTPException(status_code=500, detail={"code": "internal"})
    await state.sessions.register(claims)
    response = JSONResponse(
        {"ok": True, "token": token, "exp": expires_at, "user": user_view(account)},
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=state.config.secure_cookie,
        path="/",
    )
    return response


@router.post("/api/logout")
async def logout(request: Request, credential: Credential = Depends(require_user)) -> Response:
    state = state_of(request)
    await state.sessions.revoke(credential.claims.jti)
    await state.push_store.revoke_session(credential.claims.jti)
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@router.get("/api/session")
async def session(credential: Credential = Depends(require_user)) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "user": {"username": credential.username, "role": credential.role},
            "exp": credential.claims.expires_at,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/config")
async def config(request: Request, _: Credential = Depends(require_user)) -> JSONResponse:
    return JSONResponse(state_of(request).config_view(), headers={"Cache-Control": "no-store"})


@router.get("/api/sessions")
async def sessions(
    request: Request,
    credential: Credential = Depends(require_user),
    device_id: str | None = None,
    archived: bool | None = None,
) -> JSONResponse:
    state = state_of(request)
    # A24: the caller's own devices decide the listing, so naming another account's device_id
    # narrows the same set to nothing rather than reaching into it.
    owned = [record.device_id for record in await state.devices.list_for_user(credential.username)]
    return JSONResponse(
        {
            "sessions": await state.index.list_sessions(
                device_id=device_id, device_ids=owned, archived=archived
            )
        },
        headers={"Cache-Control": "no-store"},
    )


async def _bounded_body(request: Request, limit: int) -> dict[str, Any]:
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise HTTPException(status_code=413, detail={"code": "too_large"})
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "bad_request"}) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    return parsed
