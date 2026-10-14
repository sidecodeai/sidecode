"""FastAPI application factory.

Route order is deliberate: the API and the WebSockets are registered first, then the installer and
the client wheel, then the SPA catch-all. Every error leaves the gateway as the protocol's error
envelope, ``{"ok": false, "error": {"code", "message"}}``, so an app never has to special-case a
framework default.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .apns import ApnsProvider
from .auth_store import AuthSessionStore
from .config import Config, load_config
from .devices import DeviceStore
from .headers import SecurityHeaders
from .hub import Hub
from .index import SessionIndex
from .logging import logger
from .polish import PolishClient, Polisher
from .preference_store import PreferenceStore
from .push import PushService
from .push_store import PushStore
from .ratelimit import MAX_PER_IP, RateLimiter
from .routes import (
    device_routes,
    polish_routes,
    preference_routes,
    push_routes,
    session_routes,
    static_routes,
    stt_routes,
    user_routes,
)
from .session_registry import SessionRegistry
from .state import VERSION, GatewayState
from .stt import MimoTranscriber, OpenAiTranscriber, Transcriber
from .stt_realtime import RealtimeTranscriber
from .uploads import BoundedUploads
from .users import UserStore
from .ws import app_ws, device_ws, stt_ws

log = logger("rc_gateway.app")

ENROLL_MAX_PER_IP = 30
#: A23: an unauthenticated host may mint a claim token and poll for it this often per minute. One
#: host mints once and then holds a 25 s long poll open, which is three or four requests a minute,
#: so this leaves room for a few hosts behind one address and still refuses a flood.
PAIRING_MAX_PER_IP = 12
#: A29: polish calls spend the operator's own model credit, so one address gets this many a minute.
POLISH_MAX_PER_IP = 30
#: Speech-to-text spends the same credit and had no limit at all. Counted per one-shot upload and
#: per `/ws/stt` upgrade, never per partial: a person dictating opens one socket and the partials
#: inside it are free, so this is far above anything a user can reach by talking.
STT_MAX_PER_IP = 30
_STATUS_CODES = {
    "bad_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "too_large": 413,
    "too_many_requests": 429,
    "internal": 500,
    "unsupported": 503,
}


def build_state(
    config: Config,
    *,
    transcriber: Transcriber | None = None,
    polisher: Polisher | None = None,
    apns: ApnsProvider | None = None,
    web_sender: Any = None,
) -> GatewayState:
    """Assemble the stores, hub and push service for one gateway process."""
    auth_store = AuthSessionStore(config.db_path("auth.sqlite3"))
    state = GatewayState(
        config=config,
        devices=DeviceStore(config.db_path("devices.sqlite3")),
        index=SessionIndex(config.db_path("sessions.sqlite3")),
        push_store=PushStore(config.db_path("push.sqlite3")),
        preference_store=PreferenceStore(config.db_path("preferences.sqlite3")),
        auth_store=auth_store,
        users=UserStore(config.db_path("users.sqlite3")),
        sessions=SessionRegistry(auth_store),
        login_limiter=RateLimiter(max_per_ip=MAX_PER_IP),
        enroll_limiter=RateLimiter(max_per_ip=ENROLL_MAX_PER_IP),
        pairing_limiter=RateLimiter(max_per_ip=PAIRING_MAX_PER_IP),
        polish_limiter=RateLimiter(max_per_ip=POLISH_MAX_PER_IP),
        stt_limiter=RateLimiter(max_per_ip=STT_MAX_PER_IP),
    )
    state.apns = apns if apns is not None else _build_apns(config)
    state.push = PushService(
        state.push_store,
        device_name=state.device_name,
        badge_count=state.badge_count,
        vapid_private_key=str(config.vapid_private_pem) if config.web_push_enabled else "",
        vapid_contact=config.web_push_contact,
        web_sender=web_sender,
        apns=state.apns,
    )
    state.hub = Hub(
        state.index,
        state.devices,
        on_session_transition=state.push.on_session_transition,
        on_session_resume=state.push.on_session_resume,
        on_badge_change=state.push.on_badge_change,
        # A36: the hub asks every device for the wheel this gateway serves, so it has to be able
        # to read which one that is at any moment, not only at startup.
        served_build=state.served_build,
    )
    state.transcriber = transcriber if transcriber is not None else _build_transcriber(config)
    state.polisher = polisher if polisher is not None else _build_polisher(config)
    return state


def create_app(state: GatewayState | None = None) -> FastAPI:
    resolved = state if state is not None else build_state(load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await resolved.sessions.start()
        await resolved.hub.start()
        await resolved.push.start()
        log.info(
            "gateway ready",
            version=VERSION,
            public_origin=resolved.config.public_origin,
            stt=resolved.config.stt.provider,
            polish=resolved.config.polish.enabled,
            ios_min=resolved.config.ios_minimum_version,
            macos_min=resolved.config.macos_minimum_version,
            android_min=resolved.config.android_minimum_version,
            windows_min=resolved.config.windows_minimum_version,
            web_push=resolved.push.web_enabled,
            apns=resolved.push.apns_enabled,
        )
        try:
            yield
        finally:
            # The hub first: it lets the notifications already on their way out finish, which
            # needs the push service still running. The index last, so the sequence numbers those
            # frames recorded reach the disk.
            await resolved.hub.stop()
            await resolved.push.stop()
            await resolved.index.close()
            for service in (resolved.transcriber, resolved.polisher):
                closer = getattr(service, "close", None)
                if closer is not None:
                    await closer()

    app = FastAPI(
        title="remote-control gateway",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = resolved

    app.include_router(session_routes.router)
    app.include_router(user_routes.router)
    app.include_router(device_routes.router)
    app.include_router(preference_routes.router)
    app.include_router(push_routes.router)
    app.include_router(stt_routes.router)
    app.include_router(polish_routes.router)
    app.include_router(device_ws.router)
    app.include_router(app_ws.router)
    app.include_router(stt_ws.router)
    app.include_router(static_routes.router)

    # Ahead of routing: FastAPI would otherwise spool the whole multipart body before the route's
    # `Depends(require_user)` ever runs.
    app.add_middleware(
        BoundedUploads,
        paths=(stt_routes.UPLOAD_PATH,),
        max_bytes=stt_routes.MAX_UPLOAD_BYTES,
    )

    # Outermost, so the headers also reach the responses the middleware above writes itself. The
    # stack ships no reverse proxy, so nothing else would add them.
    app.add_middleware(SecurityHeaders, hsts=resolved.config.https_origin)

    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    return app


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    detail: Any = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code", "internal"))
        message = str(detail.get("message", _default_message(code)))
    else:
        code = _code_for_status(exc.status_code)
        message = str(detail) if detail else _default_message(code)
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": "bad_request", "message": "invalid request"}},
        status_code=400,
        headers={"Cache-Control": "no-store"},
    )


def _code_for_status(status: int) -> str:
    for code, value in _STATUS_CODES.items():
        if value == status:
            return code
    return "internal"


def _default_message(code: str) -> str:
    return code.replace("_", " ")


def _build_transcriber(config: Config) -> Transcriber | None:
    if not config.stt.enabled:
        return None
    if config.stt.provider == "mimo":
        return MimoTranscriber(config.stt)
    if config.stt.provider == "realtime":
        return RealtimeTranscriber(config.stt)
    return OpenAiTranscriber(config.stt)


def _build_polisher(config: Config) -> Polisher | None:
    """A29: the polish client exists only when the operator configured a base URL and a key."""
    return PolishClient(config.polish) if config.polish.enabled else None


def _build_apns(config: Config) -> ApnsProvider | None:
    if not config.apns.enabled:
        return None
    key_path = Path(config.apns.key_path)
    if not key_path.is_file():
        log.warning("APNs disabled: signing key not found", path=str(key_path))
        return None
    try:
        return ApnsProvider(
            config.apns.team_id,
            config.apns.key_id,
            config.apns.topic,
            key_path.read_bytes(),
            environment=config.apns.environment,
        )
    except (ValueError, OSError) as exc:
        log.warning("APNs disabled: invalid configuration", error=str(exc))
        return None


def create_configured_app() -> FastAPI:
    """Factory for uvicorn. Logging is configured by the entry point, which runs first."""
    return create_app(build_state(load_config()))
