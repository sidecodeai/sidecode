"""Web Push and APNs registration."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..apns import valid_device_token
from ..logging import logger
from ..push_store import ApnsRegistration, WebPushSubscription
from ..security import Credential, require_user, state_of
from .session_routes import _bounded_body

log = logger("rc_gateway.push")
router = APIRouter()

BODY_MAX_BYTES = 16 * 1024
_KEY = re.compile(r"[A-Za-z0-9_-]{16,1024}")
_ENVIRONMENTS = frozenset({"sandbox", "production"})


@router.get("/api/push/web/vapid")
async def vapid(request: Request, _: Credential = Depends(require_user)) -> JSONResponse:
    state = state_of(request)
    if not state.push.web_enabled:
        raise HTTPException(status_code=503, detail={"code": "unsupported"})
    return JSONResponse(
        {"public_key": state.config.vapid_public_key}, headers={"Cache-Control": "no-store"}
    )


@router.post("/api/push/web/subscribe")
async def subscribe_web(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    if not state.push.web_enabled:
        raise HTTPException(status_code=503, detail={"code": "unsupported"})
    body = await _bounded_body(request, BODY_MAX_BYTES)
    subscription = body.get("subscription")
    if not isinstance(subscription, dict):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("https://")
        or len(endpoint) > 4096
        or not isinstance(keys, dict)
    ):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not _valid_key(p256dh) or not _valid_key(auth):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    stored = await state.push_store.upsert_web(
        WebPushSubscription(
            endpoint=endpoint,
            p256dh=str(p256dh),
            auth=str(auth),
            session_jti=credential.claims.jti,
            username=credential.username,
            expires_at=float(credential.claims.expires_at),
        )
    )
    if not stored:
        log.warning("web push subscription refused: the endpoint belongs to another account")
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    log.info("web push subscription stored")
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.delete("/api/push/web/subscribe")
async def unsubscribe_web(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    body = await _bounded_body(request, BODY_MAX_BYTES)
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    await state.push_store.remove_web(endpoint, credential.username)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.post("/api/push/apns/register")
async def register_apns(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    if not state.push.apns_enabled:
        raise HTTPException(status_code=503, detail={"code": "unsupported"})
    body = await _bounded_body(request, BODY_MAX_BYTES)
    token = body.get("token")
    environment = body.get("environment")
    bundle_id = body.get("bundle_id")
    if (
        not isinstance(token, str)
        or not valid_device_token(token)
        or environment not in _ENVIRONMENTS
        or not isinstance(bundle_id, str)
        or not 1 <= len(bundle_id) <= 255
    ):
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    stored = await state.push_store.upsert_apns(
        ApnsRegistration(
            device_token=token.lower(),
            environment=str(environment),
            bundle_id=bundle_id,
            session_jti=credential.claims.jti,
            username=credential.username,
            expires_at=float(credential.claims.expires_at),
        )
    )
    if not stored:
        log.warning("apns registration refused: the device token belongs to another account")
        raise HTTPException(status_code=409, detail={"code": "conflict"})
    log.info("apns registration stored", environment=environment)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.delete("/api/push/apns/register")
async def unregister_apns(
    request: Request, credential: Credential = Depends(require_user)
) -> JSONResponse:
    state = state_of(request)
    body = await _bounded_body(request, BODY_MAX_BYTES)
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=400, detail={"code": "bad_request"})
    await state.push_store.remove_apns(token.lower(), credential.username)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


def _valid_key(value: Any) -> bool:
    return isinstance(value, str) and _KEY.fullmatch(value) is not None
