"""The live assistant's routes: whether it is on for the caller, and ``WS /ws/live``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from ..logging import logger
from ..security import (
    Credential,
    accept_for_close,
    authenticate_websocket,
    client_ip,
    require_user,
    state_of,
)
from .protocol import CLOSE_BUSY
from .service import LiveService

log = logger("rc_gateway.live.routes")
router = APIRouter()

NO_STORE = {"Cache-Control": "no-store"}


def _service(source: Request | WebSocket) -> LiveService:
    service: LiveService = source.app.state.live
    return service


@router.get("/api/live/status")
async def status(request: Request, credential: Credential = Depends(require_user)) -> JSONResponse:
    return JSONResponse(_service(request).status(credential.username), headers=NO_STORE)


@router.websocket("/ws/live")
async def live_socket(ws: WebSocket) -> None:
    """One call per socket: authenticated exactly like ``/ws/stt``, then served until it ends."""
    service = _service(ws)
    if service.upgrade_limiter.limited(client_ip(ws, state_of(ws))):
        log.warning("live upgrade rate limited")
        if await accept_for_close(ws):
            await ws.close(code=CLOSE_BUSY, reason="too_busy")
        return
    credential = await authenticate_websocket(ws, service.rejects)
    if credential is None:
        return
    try:
        await service.run_call(ws, credential)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("live socket failed")
