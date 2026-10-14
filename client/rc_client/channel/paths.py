"""Where the attachment's files live: the socket, the shim and the MCP config.

A Unix socket path is limited to 104 bytes on macOS, and `RC_CLIENT_HOME` can be
pointed anywhere, so a home that would overflow that falls back to a short
per-user directory under the system temporary directory.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

from ..config import client_home, state_dir

SOCKET_NAME = "channel.sock"
MCP_CONFIG_NAME = "claude-mcp.json"
SETTINGS_NAME = "claude-settings.json"
SHIM_NAME = "claude"
CHANNEL_SERVER_NAME = "rc"

# Leave room under the 104-byte sun_path limit for the name itself.
MAX_SOCKET_PATH_BYTES = 100


def bin_dir() -> Path:
    return client_home() / "bin"


def shim_path() -> Path:
    return bin_dir() / SHIM_NAME


def mcp_config_path() -> Path:
    return state_dir() / MCP_CONFIG_NAME


def settings_path() -> Path:
    return state_dir() / SETTINGS_NAME


def socket_path() -> Path:
    """The daemon's channel socket, shortened when the home directory is deep."""
    preferred = state_dir() / SOCKET_NAME
    if len(str(preferred).encode("utf-8")) <= MAX_SOCKET_PATH_BYTES:
        return preferred
    digest = hashlib.sha256(str(client_home()).encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"rc-{os.getuid()}-{digest}" / SOCKET_NAME


def entrypoint() -> list[str]:
    """How to call `rc-client` from wherever this code is installed."""
    script = Path(sys.executable).with_name("rc-client")
    if script.is_file() and os.access(script, os.X_OK):
        return [str(script)]
    return [sys.executable, "-m", "rc_client.cli"]


def python_executable() -> str:
    """The interpreter this installation runs on, baked into the shim (A40).

    The shim starts the pseudo-terminal proxy with it rather than with
    `rc-client`, because the proxy runs before the CLI does and a console
    script would add an import of the whole package to every terminal launch.
    """
    return sys.executable


def pty_module() -> str:
    """The proxy the shim runs Claude Code inside (A40)."""
    return "rc_client.channel.pty"


def channel_command() -> list[str]:
    """The stdio MCP server Claude Code spawns for the channel."""
    return [*entrypoint(), "channel"]


def hook_command() -> list[str]:
    """The `SessionStart` hook Claude Code runs when it enters a session."""
    return [*entrypoint(), "hook", "session-start"]


def question_hook_command() -> list[str]:
    """The `PermissionRequest` hook Claude Code runs beside its question dialog."""
    return [*entrypoint(), "hook", "permission-request"]


def path_candidates(name: str) -> list[str]:
    """Every `name` on PATH, in PATH order, skipping the directory the shim lives in.

    Both the shim and the daemon want the executable the shim wraps, and
    `shutil.which` would hand them the shim instead: `path_with_shim` puts this
    directory first so `attach_ready` can see it. An empty PATH entry means the
    working directory, which a daemon started by launchd or systemd must not
    execute from, so it is skipped rather than read as `.`.
    """
    skip = os.path.abspath(str(bin_dir()))
    return [
        os.path.join(directory, name)
        for directory in os.environ.get("PATH", "").split(os.pathsep)
        if directory and os.path.abspath(directory) != skip
    ]


def path_with_shim(base: str | None = None) -> str:
    """`base` with the shim directory in front, added at most once.

    The daemon reports `attach_ready` by resolving `claude` on its own PATH, so
    the service environment has to see the shim the installer wrote.
    """
    directory = str(bin_dir())
    entries = [item for item in (base or "").split(os.pathsep) if item and item != directory]
    return os.pathsep.join([directory, *entries])
