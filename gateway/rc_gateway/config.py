"""Gateway configuration.

One configuration entry: the repository root ``.env`` (loaded with python-dotenv when the gateway
runs from a source checkout) or plain environment variables (Docker). Secrets that the operator did
not supply are generated once into ``DATA_DIR`` so a fresh ``docker compose up -d`` works with only
``PUBLIC_ORIGIN`` and ``RC_PASSWORD``.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

from .compat import (
    ANDROID_MINIMUM_APP_VERSION,
    IOS_MINIMUM_APP_VERSION,
    MACOS_MINIMUM_APP_VERSION,
    WINDOWS_MINIMUM_APP_VERSION,
    is_release_version,
)
from .origins import canonical_origin

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_TTL_SECONDS = 30 * 24 * 3600
PAIRING_TTL_SECONDS = 600
#: The speech backends the gateway has a client for; ``none`` disables voice input. ``realtime``
#: is a live session over the OpenAI Realtime transcription protocol (Alibaba Model Studio's
#: ``qwen3-asr-flash-realtime`` speaks it), the only one that streams words as they are said.
STT_BACKENDS = ("openai", "mimo", "realtime")
STT_PROVIDERS = ("none", *STT_BACKENDS)
DEFAULT_TRUSTED_PROXIES = ("127.0.0.0/8", "::1/128")
DEFAULT_POLISH_TIMEOUT_SECONDS = 20.0


class ConfigError(RuntimeError):
    """The gateway cannot start with the configuration it was given."""


@dataclass(frozen=True)
class SttConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    #: The ``wss://`` endpoint of the ``realtime`` provider, with the model in its query when the
    #: vendor wants it there (``?model=qwen3-asr-flash-realtime``). Empty for every other provider.
    realtime_url: str = ""

    @property
    def enabled(self) -> bool:
        return self.provider in STT_BACKENDS


@dataclass(frozen=True)
class PolishConfig:
    """The operator's own OpenAI-compatible model for polishing dictation (A29).

    Both a base URL and a key are needed: a provider reached without a key would answer every
    request with an error, which is a worse setting to offer than none at all. ``models`` is an
    optional allowlist that also stands in for a provider with no ``/models`` endpoint.
    """

    base_url: str
    api_key: str
    models: tuple[str, ...]
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)


@dataclass(frozen=True)
class ApnsConfig:
    team_id: str
    key_id: str
    key_path: str
    topic: str
    environment: str

    @property
    def enabled(self) -> bool:
        return bool(self.team_id and self.key_id and self.key_path and self.topic)


@dataclass(frozen=True)
class Config:
    public_origin: str
    password: str
    secret: str
    data_dir: Path
    host: str
    port: int
    log_level: str
    web_dist_dir: Path
    client_install_script: Path
    client_dist_dir: Path
    web_push_contact: str
    stt: SttConfig
    polish: PolishConfig
    apns: ApnsConfig
    vapid_private_pem: Path
    vapid_public_key: str = ""
    #: Peers whose ``X-Forwarded-For`` the gateway believes. Loopback only by default, because a
    #: proxy that appends to the header (the standard nginx recipe does) lets a caller choose the
    #: first element and therefore its own rate-limit bucket.
    trusted_proxy_networks: tuple[str, ...] = field(default=DEFAULT_TRUSTED_PROXIES)
    #: A31: the oldest iOS app this gateway works with, and where a newer build is.
    ios_minimum_version: str = IOS_MINIMUM_APP_VERSION
    ios_update_url: str = ""
    #: A45: the same for the Mac app, whose minimum moves separately.
    macos_minimum_version: str = MACOS_MINIMUM_APP_VERSION
    macos_update_url: str = ""
    #: A46: the same for the Android and Windows apps, each moving on its own.
    android_minimum_version: str = ANDROID_MINIMUM_APP_VERSION
    android_update_url: str = ""
    windows_minimum_version: str = WINDOWS_MINIMUM_APP_VERSION
    windows_update_url: str = ""

    @property
    def https_origin(self) -> bool:
        return self.public_origin.startswith("https://")

    @property
    def secure_cookie(self) -> bool:
        return self.https_origin

    @property
    def web_push_enabled(self) -> bool:
        return bool(self.vapid_public_key and self.web_push_contact)

    def db_path(self, name: str) -> Path:
        return self.data_dir / name


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _read_or_create_secret(path: Path) -> str:
    """Return a persistent random secret, created 0600 and never visible at any wider mode."""
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(48)
    # O_EXCL with an explicit mode: the file that signs every session token must not exist at
    # 0644 for even the moment between write and chmod.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    return value


def _ensure_vapid_keys(path: Path) -> str:
    """Create a P-256 VAPID key pair once; return the base64url application server key."""
    from py_vapid import Vapid01

    if path.exists():
        vapid = Vapid01.from_file(str(path))
    else:
        vapid = Vapid01()
        vapid.generate_keys()
        # py_vapid writes with the default umask, so reserve the path at 0600 first and let it
        # write into a file that was never group- or world-readable.
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        vapid.save_key(str(path))
    point = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode("ascii")


def _stt_config() -> SttConfig:
    """Read the ``STT_*`` block, rejecting a provider the gateway has no client for."""
    provider = _env("STT_PROVIDER", "none").lower() or "none"
    if provider not in STT_PROVIDERS:
        raise ConfigError(
            f"STT_PROVIDER is not a supported provider: {provider!r} "
            f"(expected one of {', '.join(STT_PROVIDERS)})"
        )
    model = _env("STT_MODEL", "whisper-1")
    realtime_url = _realtime_url(provider, _env("STT_REALTIME_URL"), model)
    return SttConfig(
        provider=provider,
        base_url=_env("STT_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api_key=_env("STT_API_KEY"),
        model=model,
        realtime_url=realtime_url,
    )


def _realtime_url(provider: str, raw: str, model: str) -> str:
    """The live session's endpoint: required for ``realtime``, carrying the model in its query.

    Alibaba names the model in the URL (``?model=qwen3-asr-flash-realtime``); an operator who sets
    ``STT_MODEL`` and a bare URL gets the two joined, and one who wrote the model into the URL is
    left alone.
    """
    if provider != "realtime":
        return ""
    if not raw.startswith(("ws://", "wss://")):
        raise ConfigError(
            "STT_REALTIME_URL must be a ws:// or wss:// URL when STT_PROVIDER is realtime"
        )
    if "model=" in raw or not model:
        return raw
    return f"{raw}{'&' if '?' in raw else '?'}model={model}"


def _split_models(raw: str) -> tuple[str, ...]:
    """Parse ``POLISH_MODELS``, keeping the operator's order and dropping repeats."""
    models: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if candidate and candidate not in models:
            models.append(candidate)
    return tuple(models)


def _polish_timeout(raw: str) -> float:
    if not raw:
        return DEFAULT_POLISH_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ConfigError(f"POLISH_TIMEOUT_SECONDS is not a number of seconds: {raw!r}") from exc
    if seconds <= 0:
        raise ConfigError("POLISH_TIMEOUT_SECONDS must be greater than zero")
    return seconds


def _polish_config() -> PolishConfig:
    """Read the ``POLISH_*`` block. Missing values simply leave the feature off."""
    return PolishConfig(
        base_url=_env("POLISH_BASE_URL").rstrip("/"),
        api_key=_env("POLISH_API_KEY"),
        models=_split_models(_env("POLISH_MODELS")),
        timeout_seconds=_polish_timeout(_env("POLISH_TIMEOUT_SECONDS")),
    )


def _minimum_version(name: str, default: str) -> str:
    """Read ``name``, the operator's override of one app's release constant (A31, A45, A46)."""
    raw = _env(name)
    if not raw:
        return default
    if not is_release_version(raw):
        raise ConfigError(f"{name} is not a major.minor.patch version: {raw!r} (for example 1.2.0)")
    return raw


def _update_url(name: str) -> str:
    """Read ``name``, where one app's new build is (A31, A45, A46).

    Refusing a plain-http value beats shipping a link apps reject.
    """
    raw = _env(name)
    if not raw:
        return ""
    if not raw.startswith("https://"):
        raise ConfigError(
            f"{name} must be an https:// address: {raw!r} "
            "(the TestFlight, app store or download page for the new build)"
        )
    return raw


def _default_path(env_name: str, docker_path: str, repo_relative: str) -> Path:
    configured = _env(env_name)
    if configured:
        return Path(configured).expanduser()
    docker = Path(docker_path)
    if docker.exists():
        return docker
    return REPO_ROOT / repo_relative


def load_config(*, load_env_file: bool = True) -> Config:
    """Build the configuration, failing fast with an actionable message."""
    if load_env_file:
        env_file = REPO_ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)

    raw_origin = _env("PUBLIC_ORIGIN")
    if not raw_origin:
        raise ConfigError(
            "PUBLIC_ORIGIN is required: set it to the exact origin apps use, "
            "for example https://rc.example.com"
        )
    public_origin = canonical_origin(raw_origin)
    if public_origin is None:
        raise ConfigError(
            f"PUBLIC_ORIGIN is not a valid origin: {raw_origin!r} "
            "(expected scheme://host[:port] with no path)"
        )

    password = _env("RC_PASSWORD")
    if not password:
        raise ConfigError("RC_PASSWORD is required: set the login password for the admin user")

    # Before DATA_DIR is touched: a typo in the provider name should not leave secrets behind.
    stt = _stt_config()
    polish = _polish_config()
    ios_minimum_version = _minimum_version("IOS_MIN_APP_VERSION", IOS_MINIMUM_APP_VERSION)
    ios_update_url = _update_url("IOS_UPDATE_URL")
    macos_minimum_version = _minimum_version("MACOS_MIN_APP_VERSION", MACOS_MINIMUM_APP_VERSION)
    macos_update_url = _update_url("MACOS_UPDATE_URL")
    android_minimum_version = _minimum_version(
        "ANDROID_MIN_APP_VERSION", ANDROID_MINIMUM_APP_VERSION
    )
    android_update_url = _update_url("ANDROID_UPDATE_URL")
    windows_minimum_version = _minimum_version(
        "WINDOWS_MIN_APP_VERSION", WINDOWS_MINIMUM_APP_VERSION
    )
    windows_update_url = _update_url("WINDOWS_UPDATE_URL")

    data_dir = Path(_env("DATA_DIR", "/data") or "/data").expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ConfigError(f"DATA_DIR {data_dir} is not writable: {exc}") from exc

    secret = _env("RC_SECRET") or _read_or_create_secret(data_dir / "session_secret")
    vapid_private_pem = data_dir / "vapid_private.pem"
    web_push_contact = _env("WEB_PUSH_CONTACT")
    vapid_public_key = _ensure_vapid_keys(vapid_private_pem) if web_push_contact else ""

    return Config(
        public_origin=public_origin,
        password=password,
        secret=secret,
        data_dir=data_dir,
        host=_env("RC_HOST", "0.0.0.0"),
        port=int(_env("RC_PORT", "8787") or "8787"),
        log_level=_env("LOG_LEVEL", "info").lower() or "info",
        web_dist_dir=_default_path("WEB_DIST_DIR", "/app/web/dist", "web/dist"),
        client_install_script=_default_path(
            "CLIENT_INSTALL_SCRIPT", "/app/client/install.sh", "client/install.sh"
        ),
        client_dist_dir=_default_path("CLIENT_DIST_DIR", "/app/client/dist", "client/dist"),
        web_push_contact=web_push_contact,
        stt=stt,
        polish=polish,
        apns=ApnsConfig(
            team_id=_env("APNS_TEAM_ID"),
            key_id=_env("APNS_KEY_ID"),
            key_path=_env("APNS_KEY_PATH"),
            topic=_env("APNS_TOPIC"),
            environment=_env("APNS_ENVIRONMENT", "production").lower() or "production",
        ),
        vapid_private_pem=vapid_private_pem,
        vapid_public_key=vapid_public_key,
        trusted_proxy_networks=_trusted_proxies(_env("TRUSTED_PROXIES")),
        ios_minimum_version=ios_minimum_version,
        ios_update_url=ios_update_url,
        macos_minimum_version=macos_minimum_version,
        macos_update_url=macos_update_url,
        android_minimum_version=android_minimum_version,
        android_update_url=android_update_url,
        windows_minimum_version=windows_minimum_version,
        windows_update_url=windows_update_url,
    )


def _trusted_proxies(raw: str) -> tuple[str, ...]:
    """Parse ``TRUSTED_PROXIES``, ignoring entries that are not networks."""
    if not raw:
        return DEFAULT_TRUSTED_PROXIES
    networks: list[str] = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            continue
        networks.append(candidate)
    return tuple(networks) or DEFAULT_TRUSTED_PROXIES
