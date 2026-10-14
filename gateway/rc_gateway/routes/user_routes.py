"""Registration, the caller's own password, and the admin's account routes (§3.1, §3.2, §3.9).

Everything an account owns hangs off its username: devices, the sessions on them, pairing codes,
push registrations and login sessions. Disabling one therefore has to reach all of that at once,
and deleting one has to erase it, which is why both paths go through the same device removal the
``DELETE /api/devices/{device_id}`` route uses.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..accounts import (
    ADMIN_USERNAME,
    ROLE_MEMBER,
    STATE_DISABLED,
    normalize_username,
    password_allowed,
    role_allowed,
    state_allowed,
    username_allowed,
    verify_hash,
)
from ..auth import SESSION_COOKIE_NAME
from ..frames import CLOSE_FORBIDDEN
from ..logging import logger
from ..security import (
    Credential,
    client_ip,
    reject_foreign_origin,
    require_admin,
    require_user,
    state_of,
)
from ..state import GatewayState
from ..users import UserRecord
from ..views import user_record_view
from .device_routes import remove_device
from .session_routes import LOGIN_BODY_MAX_BYTES, _bounded_body, issue_session

log = logger("rc_gateway.users")
router = APIRouter()

BODY_MAX_BYTES = 4096


@router.post("/api/register")
async def register(request: Request) -> Response:
    """Make a member account and sign it in, while the admin allows it (A24)."""
    state = state_of(request)
    reject_foreign_origin(request)
    if state.login_limiter.limited(client_ip(request, state)):
        log.warning("registration rate limited")
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})
    if not await state.users.registration_open():
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    body = await _bounded_body(request, LOGIN_BODY_MAX_BYTES)
    username, password = _credentials(body)
    account = await _create_account(state, username, password, ROLE_MEMBER)
    await state.users.touch_login(account.username)
    log.info("account registered", username=account.username)
    return await issue_session(state, account)


@router.post("/api/password")
async def change_password(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    """Change the caller's own password. The operator's lives in ``RC_PASSWORD`` (§3.2)."""
    state = state_of(request)
    if credential.username == ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    body = await _bounded_body(request, BODY_MAX_BYTES)
    current = body.get("current_password")
    new_password = body.get("new_password")
    if not isinstance(current, str) or not isinstance(new_password, str):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    if not password_allowed(new_password):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    account = await state.users.get(credential.username)
    if account is None:
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})
    if not await verify_hash(current, account.password_hash):
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})
    await state.users.set_password(account.username, new_password)
    log.info("password changed", username=account.username)
    # Other sign-ins stay valid: a phone must not be signed out because a laptop changed this.
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.get("/api/users")
async def list_users(request: Request, _: Credential = Depends(require_admin)) -> JSONResponse:
    state = state_of(request)
    counts = await state.devices.counts_by_user()
    return JSONResponse(
        {
            "users": [
                user_record_view(record, counts.get(record.username, 0))
                for record in await state.users.list_accounts()
            ],
            "registration_open": await state.users.registration_open(),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/users")
async def create_user(request: Request, _: Credential = Depends(require_admin)) -> JSONResponse:
    state = state_of(request)
    body = await _bounded_body(request, BODY_MAX_BYTES)
    username, password = _credentials(body)
    role = body.get("role", ROLE_MEMBER)
    if not isinstance(role, str) or not role_allowed(role):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    account = await _create_account(state, username, password, role)
    log.info("account created", username=account.username, role=role)
    return await _user_response(state, account.username)


@router.patch("/api/users/{username}")
async def patch_user(
    username: str, request: Request, _: Credential = Depends(require_admin)
) -> JSONResponse:
    state = state_of(request)
    account = await _existing(state, username)
    state_value, role, password = _patch_fields(await _bounded_body(request, BODY_MAX_BYTES))
    if state_value is None and role is None and password is None:
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    # The operator is the one account that cannot be locked out of its own gateway, and its
    # password is `RC_PASSWORD` rather than anything this route could store.
    if account.username == ADMIN_USERNAME and (
        state_value == STATE_DISABLED or role == ROLE_MEMBER or password is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    if password is not None:
        await state.users.set_password(account.username, password)
    if role is not None:
        await state.users.set_role(account.username, role)
    if state_value is not None:
        await state.users.set_state(account.username, state_value)
        if state_value == STATE_DISABLED:
            await _lock_out(state, account.username)
    log.info("account updated", username=account.username)
    return await _user_response(state, account.username)


@router.delete("/api/users/{username}")
async def delete_user(
    username: str, request: Request, _: Credential = Depends(require_admin)
) -> JSONResponse:
    state = state_of(request)
    account = await _existing(state, username)
    if account.username == ADMIN_USERNAME:
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    await _delete_account(state, account)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.delete("/api/account")
async def delete_account(
    request: Request,
    credential: Credential = Depends(require_user),
) -> JSONResponse:
    state = state_of(request)
    reject_foreign_origin(request)
    if state.login_limiter.limited(client_ip(request, state)):
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})
    if credential.username == ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    body = await _bounded_body(request, BODY_MAX_BYTES)
    password = body.get("current_password")
    if not isinstance(password, str) or set(body) != {"current_password"}:
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    account = await _existing(state, credential.username)
    if not await verify_hash(password, account.password_hash):
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})
    await _delete_account(state, account)
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


async def _delete_account(state: GatewayState, account: UserRecord) -> None:
    # Disable first so a login racing cleanup cannot obtain another valid session.
    await state.users.set_state(account.username, STATE_DISABLED)
    for record in await state.devices.list_for_user(account.username):
        await remove_device(state, record.device_id, account.username)
    await state.devices.drop_pairings_for_user(account.username)
    await state.push_store.remove_for_user(account.username)
    await state.preference_store.remove_for_user(account.username)
    await state.sessions.revoke_for_user(account.username)
    await state.users.delete(account.username)
    log.info("account deleted", username=account.username)


@router.patch("/api/registration")
async def patch_registration(
    request: Request, _: Credential = Depends(require_admin)
) -> JSONResponse:
    state = state_of(request)
    body = await _bounded_body(request, BODY_MAX_BYTES)
    open_ = body.get("open")
    if not isinstance(open_, bool):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    await state.users.set_registration_open(open_)
    log.info("registration setting changed", open=open_)
    return JSONResponse({"open": open_}, headers={"Cache-Control": "no-store"})


async def _lock_out(state: GatewayState, username: str) -> None:
    """Disabling takes effect now: every token is revoked and every device socket closes (§3.9)."""
    await state.sessions.revoke_for_user(username)
    for record in await state.devices.list_for_user(username):
        await state.hub.disconnect_device(
            record.device_id, reason="account disabled", code=CLOSE_FORBIDDEN
        )


async def _create_account(
    state: GatewayState, username: str, password: str, role: str
) -> UserRecord:
    if await state.users.create(username, password, role) is not None:
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    return await _reread(state, username)


async def _reread(state: GatewayState, username: str) -> UserRecord:
    account = await state.users.get(username)
    if account is None:
        raise HTTPException(status_code=500, detail={"code": "internal"})
    return account


async def _existing(state: GatewayState, username: str) -> UserRecord:
    account = await state.users.get(normalize_username(username))
    if account is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return account


async def _user_response(state: GatewayState, username: str) -> JSONResponse:
    account = await _reread(state, username)
    devices = await state.devices.count_for_user(account.username)
    return JSONResponse(
        {"user": user_record_view(account, devices)}, headers={"Cache-Control": "no-store"}
    )


def _credentials(body: dict[str, Any]) -> tuple[str, str]:
    """The username and password of a body that creates an account, against the rules of §3.1."""
    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    normalized = normalize_username(username)
    if not username_allowed(normalized) or not password_allowed(password):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    return normalized, password


def _patch_fields(body: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Validate whichever of ``state``, ``role`` and ``password`` a patch carries."""
    state_value = _optional(body, "state", state_allowed)
    role = _optional(body, "role", role_allowed)
    password = _optional(body, "password", password_allowed)
    return state_value, role, password


def _optional(body: dict[str, Any], key: str, allowed: Callable[[str], bool]) -> str | None:
    if key not in body:
        return None
    value = body[key]
    if not isinstance(value, str) or not allowed(value):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    return str(value)
