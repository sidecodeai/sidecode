"""Device configuration: `~/.rc-client/config.toml` plus the state and log dirs.

`RC_CLIENT_HOME` overrides the directory (used by tests and by side-by-side
installs). The credential file is written atomically with mode 0600.
"""

from __future__ import annotations

import ipaddress
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tomli_w

from .errors import RcError
from .proxy import DIRECT, normalise_proxy

CONFIG_FILENAME = "config.toml"

# Mirroring defaults: enough history to be useful without flooding a fresh
# device with years of past sessions on the day it is enrolled.
DEFAULT_MIRROR_MAX_SESSIONS = 50
DEFAULT_MIRROR_MAX_AGE_DAYS = 14

# The Claude CLI loads user-level settings by default, and those can carry
# auto-approval (`permissions.defaultMode: "auto"`, PermissionRequest hooks).
# A remote session must never be approved by the machine on behalf of a user
# who is not looking at it, so the user source is left out unless asked for.
DEFAULT_CLAUDE_SETTING_SOURCES = ("project", "local")


def client_home() -> Path:
    override = os.environ.get("RC_CLIENT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".rc-client"


def config_path() -> Path:
    return client_home() / CONFIG_FILENAME


def state_dir() -> Path:
    return client_home() / "state"


def log_dir() -> Path:
    return client_home() / "logs"


def database_path() -> Path:
    return state_dir() / "rc-client.sqlite3"


@dataclass(slots=True)
class MirrorConfig:
    """How much terminal history a device imports when it starts."""

    max_sessions: int = DEFAULT_MIRROR_MAX_SESSIONS
    max_age_days: int = DEFAULT_MIRROR_MAX_AGE_DAYS

    def to_dict(self) -> dict[str, int]:
        return {"max_sessions": self.max_sessions, "max_age_days": self.max_age_days}


@dataclass(slots=True)
class ClaudeConfig:
    setting_sources: list[str] = field(default_factory=lambda: list(DEFAULT_CLAUDE_SETTING_SOURCES))

    def to_dict(self) -> dict[str, list[str]]:
        return {"setting_sources": list(self.setting_sources)}


@dataclass(slots=True)
class TerminalConfig:
    """Whether this device offers a shell over the gateway (A38, PROTOCOL 7.3)."""

    enabled: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {"enabled": self.enabled}


@dataclass(slots=True)
class Config:
    gateway_origin: str
    device_id: str
    device_token: str
    name: str
    # How the gateway is reached: `""` directly, or one http/https proxy URL.
    # `enroll --proxy env` resolves the environment's answer and stores that URL,
    # so nothing here depends on the service's environment. See rc_client/proxy.py.
    proxy: str = DIRECT
    mirror: MirrorConfig = field(default_factory=MirrorConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    terminal: TerminalConfig = field(default_factory=TerminalConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gateway_origin": self.gateway_origin,
            "device_id": self.device_id,
            "device_token": self.device_token,
            "name": self.name,
            "proxy": self.proxy,
            "mirror": self.mirror.to_dict(),
            "claude": self.claude.to_dict(),
            "terminal": self.terminal.to_dict(),
        }

    @property
    def device_ws_url(self) -> str:
        split = urlsplit(self.gateway_origin)
        scheme = "wss" if split.scheme == "https" else "ws"
        return f"{scheme}://{split.netloc}/ws/device"


def is_local_origin(origin: str) -> bool:
    """True for loopback and private hosts, where plain http:// is acceptable.

    The host must be a literal address: a name like `192.168.evil.com` resolves
    wherever its owner points it, so it is treated as public.
    """
    host = (urlsplit(origin).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def normalise_origin(origin: str) -> str:
    """Validate a gateway origin and strip any path/query suffix."""
    value = origin.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    split = urlsplit(value)
    if split.scheme not in {"http", "https"} or not split.netloc:
        raise RcError("bad_request", f"invalid gateway origin: {origin}")
    if split.scheme == "http" and not is_local_origin(value):
        raise RcError(
            "bad_request",
            "refusing a plain http:// gateway that is not loopback or a private address",
        )
    return f"{split.scheme}://{split.netloc}"


def ensure_dirs() -> None:
    """Create the client directories, tightening any that already exist.

    `mkdir(mode=…)` is a no-op on an existing directory, so a home restored from
    a backup could otherwise leave the credential file world-readable.
    """
    for directory in (client_home(), state_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            os.chmod(directory, 0o700)


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write `data` to `path` atomically, never leaving a readable partial file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save_config(config: Config) -> Path:
    ensure_dirs()
    target = config_path()
    write_atomic(target, tomli_w.dumps(config.to_dict()).encode("utf-8"))
    return target


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        raise RcError(
            "not_found",
            f"no device configuration at {path}; run `rc-client enroll --gateway URL --pair CODE`",
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    missing = [key for key in ("gateway_origin", "device_id", "device_token") if not raw.get(key)]
    if missing:
        raise RcError("bad_request", f"{path} is missing: {', '.join(missing)}")
    return Config(
        gateway_origin=str(raw["gateway_origin"]).rstrip("/"),
        device_id=str(raw["device_id"]),
        device_token=str(raw["device_token"]),
        name=str(raw.get("name") or ""),
        proxy=normalise_proxy(str(raw.get("proxy") or "")),
        mirror=_mirror_config(raw.get("mirror")),
        claude=_claude_config(raw.get("claude")),
        terminal=_terminal_config(raw.get("terminal")),
    )


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _mirror_config(raw: Any) -> MirrorConfig:
    section = raw if isinstance(raw, dict) else {}
    return MirrorConfig(
        max_sessions=_positive_int(section.get("max_sessions"), DEFAULT_MIRROR_MAX_SESSIONS),
        max_age_days=_positive_int(section.get("max_age_days"), DEFAULT_MIRROR_MAX_AGE_DAYS),
    )


def _claude_config(raw: Any) -> ClaudeConfig:
    section = raw if isinstance(raw, dict) else {}
    sources = section.get("setting_sources")
    if not isinstance(sources, list):
        return ClaudeConfig()
    allowed = {"user", "project", "local"}
    return ClaudeConfig(setting_sources=[str(item) for item in sources if str(item) in allowed])


def _terminal_config(raw: Any) -> TerminalConfig:
    """A terminal is offered unless the configuration says otherwise (A38)."""
    section = raw if isinstance(raw, dict) else {}
    enabled = section.get("enabled")
    return TerminalConfig(enabled=enabled if isinstance(enabled, bool) else True)


def config_exists() -> bool:
    return config_path().exists()
