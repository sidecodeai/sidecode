"""EXPERIMENTAL voice supervisor assistant (see docs/LIVE_ASSISTANT.md).

Off unless ``LIVE_ASSISTANT_ENABLED`` and ``LIVE_OPENAI_API_KEY`` are set. The browser talks only
to the gateway (``WS /ws/live``); the gateway holds the one WebSocket to the Live API. Who may call,
and what a call costs, is a policy (``policy.py``): a self-hosted gateway gets the allowlist and the
daily cap, and a gateway that bills its calls passes its own. Removing the feature means deleting
this package and the few mount points outside it, which the document lists under "Where it lives
and how to remove it".
"""

from __future__ import annotations

from fastapi import FastAPI

from ..logging import logger
from ..state import GatewayState
from .config import LiveConfig, LiveConfigError, load_live_config
from .policy import LivePolicy
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
    policy: LivePolicy | None = None,
) -> LiveService | None:
    """Register the routes when the feature is on, under ``policy`` (the self-hosted one if none).

    A state with a paid service is the official gateway, whose calls are billed: without a policy
    that bills them the feature stays off, rather than run for nothing. One without the attribute
    at all (the open-source tree has no paid package) is a self-hosted one.
    """
    if getattr(state, "paid", None) is not None and policy is None:
        return None
    try:
        resolved = config if config is not None else load_live_config()
    except LiveConfigError as exc:
        log.error("live assistant stays off", reason=str(exc))
        return None
    if resolved is None:
        return None
    service = LiveService(
        state, resolved, connector if connector is not None else connect_upstream, policy=policy
    )
    app.state.live = service
    app.include_router(router)
    return service
