"""Live assistant settings, read from the gateway's own environment (and so its single ``.env``)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..accounts import normalize_username

DEFAULT_BASE_URL = "https://api.openai.com"
DEFAULT_MODEL = "gpt-live-1"
DEFAULT_BACKEND_MODEL = "gpt-6-luna"
DEFAULT_MAX_CONCURRENT_CALLS = 2
DEFAULT_COOLDOWN_SECONDS = 10.0
DEFAULT_IDLE_SECONDS = 90.0
DEFAULT_MAX_CALL_SECONDS = 1200.0
DEFAULT_DAILY_SECONDS = 7200.0

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class LiveConfigError(ValueError):
    """A live assistant setting that cannot be used. The feature stays off and says why."""


@dataclass(frozen=True)
class LiveConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    backend_model: str = DEFAULT_BACKEND_MODEL
    #: Left out of the session configuration unless the operator names one.
    voice: str = ""
    idle_seconds: float = DEFAULT_IDLE_SECONDS
    max_call_seconds: float = DEFAULT_MAX_CALL_SECONDS
    #: Call time one account may use per UTC day.
    daily_seconds: float = DEFAULT_DAILY_SECONDS
    #: The only accounts that may use the feature; empty means nobody.
    allowed_users: frozenset[str] = frozenset()
    max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    #: Whether the gateway cuts the assistant off when it hears the person talk over it.
    barge_in: bool = True

    @property
    def socket_url(self) -> str:
        """The Live API's WebSocket endpoint. The model is named in ``session.start``, not here."""
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}/v1/live/sessions"


def _seconds(raw: str, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _positive_seconds(raw: str, default: float) -> float:
    value = _seconds(raw, default)
    return value if value > 0 else default


def _count(raw: str, default: int) -> int:
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _flag(raw: str, default: bool) -> bool:
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    return False if lowered in _FALSE else default


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _base_url(raw: str) -> str:
    """The API root: TLS anywhere, or no TLS on a loopback address only (a test double)."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LiveConfigError("LIVE_OPENAI_BASE_URL must be an http(s) URL")
    if parts.username or parts.password:
        raise LiveConfigError("LIVE_OPENAI_BASE_URL must not carry credentials")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise LiveConfigError("LIVE_OPENAI_BASE_URL may use http only on a loopback address")
    return raw.rstrip("/")


def load_live_config(env: Mapping[str, str] | None = None) -> LiveConfig | None:
    """The settings, or ``None`` while the feature is off or has no key.

    Raises ``LiveConfigError`` for a setting that would send the key somewhere it must not go.
    """
    values = os.environ if env is None else env

    def read(name: str, default: str = "") -> str:
        return (values.get(name) or default).strip()

    if read("LIVE_ASSISTANT_ENABLED").lower() not in _TRUE:
        return None
    key = read("LIVE_OPENAI_API_KEY")
    if not key:
        return None
    return LiveConfig(
        api_key=key,
        base_url=_base_url(read("LIVE_OPENAI_BASE_URL", DEFAULT_BASE_URL)),
        model=read("LIVE_MODEL", DEFAULT_MODEL),
        backend_model=read("LIVE_BACKEND_MODEL", DEFAULT_BACKEND_MODEL),
        voice=read("LIVE_VOICE"),
        idle_seconds=_positive_seconds(read("LIVE_IDLE_TIMEOUT_SECONDS"), DEFAULT_IDLE_SECONDS),
        max_call_seconds=_positive_seconds(read("LIVE_MAX_CALL_SECONDS"), DEFAULT_MAX_CALL_SECONDS),
        daily_seconds=_positive_seconds(read("LIVE_DAILY_SECONDS"), DEFAULT_DAILY_SECONDS),
        allowed_users=frozenset(
            normalize_username(name)
            for name in read("LIVE_ALLOWED_USERS").split(",")
            if name.strip()
        ),
        max_concurrent_calls=_count(
            read("LIVE_MAX_CONCURRENT_CALLS"), DEFAULT_MAX_CONCURRENT_CALLS
        ),
        cooldown_seconds=_seconds(read("LIVE_COOLDOWN_SECONDS"), DEFAULT_COOLDOWN_SECONDS),
        barge_in=_flag(read("LIVE_BARGE_IN"), True),
    )
