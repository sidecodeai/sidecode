"""Entry point for the ``rc-gateway`` console script."""

from __future__ import annotations

import sys

import uvicorn

from .config import Config, ConfigError, load_config
from .frames import PING_INTERVAL_SECONDS, SILENT_TIMEOUT_SECONDS, WS_MAX_MESSAGE_BYTES
from .logging import configure_logging, logger, uvicorn_log_config

log = logger("rc_gateway.main")


def server_config(config: Config) -> uvicorn.Config:
    """How the WebSocket server itself is set up, separately so a test can read it back."""
    return uvicorn.Config(
        "rc_gateway.app:create_configured_app",
        factory=True,
        host=config.host,
        port=config.port,
        log_config=uvicorn_log_config(config.log_level),
        access_log=False,
        ws_max_size=WS_MAX_MESSAGE_BYTES,
        # Amendment A13. The application pings in frames.py are the only keepalive on the link.
        # uvicorn's default is a second, shorter clock — 20 s ping with a 20 s answer deadline —
        # and a device whose event loop stalls for twenty seconds is well inside the 90 s the
        # contract allows, but was being closed with 1011 and reported offline the same instant.
        ws_ping_interval=None,
        ws_ping_timeout=None,
        proxy_headers=False,
        # The gateway advertises no version banner; SecurityHeaders sets the rest.
        server_header=False,
    )


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"rc-gateway: {exc}", file=sys.stderr)
        return 2
    configure_logging(config.log_level)
    server = server_config(config)
    # Read back from the config rather than repeating the arguments, so the line says what the
    # server will actually do with a future uvicorn that refuses to disable its own clock.
    log.info(
        "websocket keepalive",
        server_ping_interval=server.ws_ping_interval,
        server_ping_timeout=server.ws_ping_timeout,
        gateway_ping_interval=PING_INTERVAL_SECONDS,
        gateway_silent_timeout=SILENT_TIMEOUT_SECONDS,
    )
    uvicorn.Server(server).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
