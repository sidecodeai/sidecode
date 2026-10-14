"""Redeem a pairing code at the gateway and store the device credentials."""

from __future__ import annotations

import platform
import socket
from typing import Any

import httpx

from . import __version__
from .config import Config, normalise_origin, save_config
from .errors import RcError
from .logging_setup import logger
from .models import AgentInfo
from .proxy import httpx_options

log = logger("rc_client.enroll")

ENROLL_TIMEOUT = 30.0


def device_facts(name: str | None) -> dict[str, Any]:
    hostname = socket.gethostname()
    machine = platform.machine()
    return {
        "name": name or hostname,
        "platform": "macos" if platform.system() == "Darwin" else "linux",
        "hostname": hostname,
        "arch": "arm64" if machine in {"arm64", "aarch64"} else "x86_64",
        "client_version": __version__,
    }


async def enroll(
    gateway: str,
    code: str,
    name: str | None,
    agents: list[AgentInfo],
    *,
    proxy: str,
) -> Config:
    """POST /api/devices/enroll and persist the returned credentials.

    `proxy` is how this host reaches the gateway (`""` directly, or a URL; see
    rc_client/proxy.py). It is saved with the credentials, so the daemon and the
    updater dial the same way; every caller states it, because a call that let it
    default would quietly enroll a proxied host into a direct dial.
    """
    origin = normalise_origin(gateway)
    payload = {
        "code": code.strip().upper(),
        **device_facts(name),
        "agents": [info.to_dict() for info in agents],
    }
    try:
        # Enrollment talks to the same gateway as the WebSocket link and reaches
        # it the same way: directly unless `proxy` says otherwise.
        async with httpx.AsyncClient(timeout=ENROLL_TIMEOUT, **httpx_options(proxy)) as client:
            response = await client.post(f"{origin}/api/devices/enroll", json=payload)
    except httpx.HTTPError as exc:
        raise RcError("internal", f"cannot reach {origin}: {type(exc).__name__}") from exc

    if response.status_code == 404:
        raise RcError("not_found", "the pairing code is unknown or has expired")
    if response.status_code == 409:
        raise RcError("conflict", "the pairing code has already been used")
    if response.status_code >= 400:
        raise RcError("internal", f"enrollment failed with HTTP {response.status_code}")

    try:
        body = response.json()
    except ValueError as exc:
        raise RcError("internal", "the gateway returned a malformed enrollment response") from exc
    device_id = str(body.get("device_id") or "")
    token = str(body.get("device_token") or "")
    if not device_id or not token:
        raise RcError("internal", "the gateway response is missing device credentials")

    config = Config(
        gateway_origin=origin,
        device_id=device_id,
        device_token=token,
        name=str(payload["name"]),
        proxy=proxy,
    )
    save_config(config)
    log.info("device enrolled", device=device_id, gateway=origin)
    return config
