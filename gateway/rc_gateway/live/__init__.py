"""EXPERIMENTAL voice supervisor assistant (see docs/LIVE_ASSISTANT.md).

Self-hosted only and off unless ``LIVE_ASSISTANT_ENABLED`` and ``LIVE_OPENAI_API_KEY`` are set.
The browser talks only to the gateway (``WS /ws/live``); the gateway holds the one WebSocket to the
Live API. Removing the feature means deleting this package and the few mount points outside it,
which the document lists under "Where it lives and how to remove it".
"""

from __future__ import annotations

from fastapi import FastAPI

from ..logging import logger
from ..state import GatewayState
from .config import LiveConfig, LiveConfigError, load_live_config
from .routes import router
from .service import LiveService
from .upstream import UpstreamConnector, connect_upstream

log = logger("rc_gateway.live")


def mount_live(
    app: FastAPI,
    state: GatewayState,
    *,
    config: LiveConfig | None = None,
    connector: UpstreamConnector | None = None,
) -> LiveService | None:
    """Register the routes when the feature is on. The official gateway never mounts it."""
    # A state with a paid service is the official gateway; one without the attribute at all
    # (the open-source tree has no paid package) is a self-hosted one.
    if getattr(state, "paid", None) is not None:
        return None
    try:
        resolved = config if config is not None else load_live_config()
    except LiveConfigError as exc:
        log.error("live assistant stays off", reason=str(exc))
        return None
    if resolved is None:
        return None
    service = LiveService(state, resolved, connector if connector is not None else connect_upstream)
    app.state.live = service
    app.include_router(router)
    return service
