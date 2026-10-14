"""Device listing, pairing codes, enrollment and revocation."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import PAIRING_TTL_SECONDS
from ..logging import logger
from ..origins import websocket_url
from ..pairing_requests import TooManyWaiters, is_claim_token
from ..security import Credential, client_ip, require_user, state_of
from ..state import GatewayState
from ..views import device_view
from .session_routes import _bounded_body

log = logger("rc_gateway.devices")
router = APIRouter()

ENROLL_BODY_MAX_BYTES = 16 * 1024
MAX_NAME_LENGTH = 64
MAX_FIELD_LENGTH = 128


@router.get("/api/devices")
async def list_devices(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    records = await state.devices.list_for_user(credential.username)
    return JSONResponse(
        {
            "devices": [
                device_view(
                    record,
                    online=state.hub.device_online(record.device_id),
                    latency_ms=state.hub.latency_for(record.device_id),
                )
                for record in records
            ]
        },
        headers={"Cache-Control": "no-store"},
    )


@router.patch("/api/devices/{device_id}")
async def rename_device(
    device_id: str, request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    body = await _bounded_body(request, ENROLL_BODY_MAX_BYTES)
    name = _text(body.get("name"), MAX_NAME_LENGTH)
    if not name:
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    if not await state.devices.rename(device_id, credential.username, name):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    record = await state.devices.get(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    device = device_view(
        record,
        online=state.hub.device_online(device_id),
        latency_ms=state.hub.latency_for(device_id),
    )
    await state.hub.broadcast_user(
        credential.username, {"type": "device.updated", "device": device}
    )
    return JSONResponse({"device": device}, headers={"Cache-Control": "no-store"})


@router.delete("/api/devices/{device_id}")
async def delete_device(
    device_id: str, request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    if not await remove_device(state_of(request), device_id, credential.username):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


async def remove_device(state: GatewayState, device_id: str, username: str) -> bool:
    """Revoke one device and erase what the gateway held for it.

    Deleting an account does this for every device it owns (§3.9), so the two paths cannot drift:
    the token stops working, the socket closes, the sessions leave the index and the app that was
    looking at them is told.
    """
    if not await state.devices.revoke(device_id, username):
        return False
    await state.hub.disconnect_device(device_id, reason="device revoked")
    await state.hub.forget_device_sessions(device_id, username)
    await state.hub.broadcast_user(username, {"type": "device.removed", "device_id": device_id})
    log.info("device revoked", device_id=device_id)
    return True


@router.post("/api/devices/pairing")
async def create_pairing(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    grant = await state.devices.create_pairing(credential.username, ttl=PAIRING_TTL_SECONDS)
    await state.hub.pairing_started(grant.code, credential.username)
    command = f"curl -fsSL {state.config.public_origin}/install.sh | sh -s -- --pair {grant.code}"
    return JSONResponse(
        {
            "code": grant.code,
            "expires_at": grant.expires_at * 1000,
            "install": {"macos": command, "linux": command},
        },
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/api/devices/pairing/{code}")
async def cancel_pairing(
    code: str, request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    # The hub's pairing progress is not account-scoped, so it is only ever cleared once the store
    # has confirmed the code was this caller's: otherwise naming someone else's code would stop
    # their `pairing.progress` frames and hang their pairing screen on "waiting".
    if not await state.devices.cancel_pairing(code, credential.username):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    await state.hub.pairing_cancelled(code)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.post("/api/pairing/requests")
async def create_pairing_request(request: Request) -> JSONResponse:
    """A host asks to be claimed by scanning (A23). Unauthenticated: the token grants nothing."""
    state = state_of(request)
    address = client_ip(request, state)
    if state.pairing_limiter.limited(address):
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})
    pending = state.pairing_requests.mint(address)
    if pending is None:
        log.warning("pairing request refused: this address holds too many claim tokens")
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})
    return JSONResponse(
        {
            "token": pending.token,
            "expires_at": pending.expires_at * 1000,
            "claim_url": f"{state.config.public_origin}/pair#{pending.token}",
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/pairing/requests/{token}")
async def pairing_request_status(token: str, request: Request) -> JSONResponse:
    """The host's long poll: held open until the token is claimed, then the code, exactly once.

    Unauthenticated like the mint, and limited like it: each call parks a task for 25 s, so the
    address is rate limited and the token itself allows only a couple of parked polls at a time.
    """
    state = state_of(request)
    if state.pairing_limiter.limited(client_ip(request, state)):
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})
    pending = state.pairing_requests.find(token) if is_claim_token(token) else None
    if pending is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    if pending.expired(int(time.time())):
        state.pairing_requests.spend(token)
        raise HTTPException(status_code=410, detail={"code": "not_found"})
    try:
        claimed = await state.pairing_requests.wait_for_claim(pending)
    except TooManyWaiters as exc:
        log.warning("pairing poll refused: too many polls on one claim token")
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"}) from exc
    if not claimed:
        return JSONResponse({"status": "waiting"}, headers={"Cache-Control": "no-store"})
    # Delivered exactly once: the host enrols with the code, and a replayed poll reads 404.
    state.pairing_requests.spend(token)
    return JSONResponse(
        {
            "status": "claimed",
            "code": pending.code,
            "expires_at": pending.code_expires_at * 1000,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/pairing/requests/{token}/claim")
async def claim_pairing_request(
    token: str, request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    """Bind a host's request to the signed-in user and mint its pairing code (A23)."""
    state = state_of(request)
    outcome = state.pairing_requests.begin_claim(token) if is_claim_token(token) else "not_found"
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    try:
        grant = await state.devices.create_pairing(credential.username, ttl=PAIRING_TTL_SECONDS)
    except Exception:
        state.pairing_requests.abandon(token)
        raise
    await state.hub.pairing_started(grant.code, credential.username)
    state.pairing_requests.fulfil(token, grant.code, grant.expires_at)
    log.info("pairing request claimed")
    return JSONResponse(
        {"code": grant.code, "expires_at": grant.expires_at * 1000},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/devices/enroll")
async def enroll(request: Request) -> JSONResponse:
    """Redeem a pairing code. The code is the credential, so no session is required."""
    state = state_of(request)
    address = client_ip(request, state)
    if state.enroll_limiter.limited(address):
        raise HTTPException(status_code=429, detail={"code": "too_many_requests"})
    body = await _bounded_body(request, ENROLL_BODY_MAX_BYTES)
    code = _text(body.get("code"), MAX_FIELD_LENGTH)
    name = _text(body.get("name"), MAX_NAME_LENGTH) or "device"
    if not code:
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    outcome = await state.devices.redeem(
        code,
        name=name,
        platform=_text(body.get("platform"), MAX_FIELD_LENGTH),
        hostname=_text(body.get("hostname"), MAX_FIELD_LENGTH),
        arch=_text(body.get("arch"), MAX_FIELD_LENGTH),
        client_version=_text(body.get("client_version"), MAX_FIELD_LENGTH),
        agents=_agents(body.get("agents")),
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    if outcome == "conflict":
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    if isinstance(outcome, str):
        raise HTTPException(status_code=500, detail={"code": "internal"})

    log.info("device enrolled", device_id=outcome.device_id)
    await state.hub.pairing_enrolled(code, outcome.device_id)
    return JSONResponse(
        {
            "device_id": outcome.device_id,
            "device_token": outcome.token,
            "gateway_ws_url": websocket_url(state.config.public_origin, "/ws/device"),
        },
        headers={"Cache-Control": "no-store"},
    )


def _agents(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value[:16] if isinstance(item, dict)]


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127).strip()
    return cleaned[:limit]
