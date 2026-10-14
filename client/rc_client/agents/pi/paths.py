"""Where the pi extension lives, and the socket it dials.

The socket sits beside the channel socket in the device home and follows the
same rule: a home deep enough to overflow the 104-byte `sun_path` limit falls
back to a short per-user directory under the system temporary directory. The
extension derives the same path for itself, so the rule is stated once here and
mirrored in `extension/remote-control.ts`.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from ...channel.paths import MAX_SOCKET_PATH_BYTES
from ...config import client_home, state_dir

SOCKET_NAME = "pi-extension.sock"
EXTENSION_NAME = "remote-control.ts"
# The environment variable the device sets on the RPC children it starts, so
# they never have to derive anything.
SOCKET_ENV = "RC_PI_SOCKET"
# What the device tells a child its session's permission mode is at startup.
MODE_ENV = "RC_PI_PERMISSION_MODE"


def socket_path() -> Path:
    """The socket the extension dials, shortened when the home directory is deep."""
    preferred = state_dir() / SOCKET_NAME
    if len(str(preferred).encode("utf-8")) <= MAX_SOCKET_PATH_BYTES:
        return preferred
    digest = hashlib.sha256(str(client_home()).encode("utf-8")).hexdigest()[:10]
    return Path(tempfile.gettempdir()) / f"rc-{os.getuid()}-{digest}" / SOCKET_NAME


def bundled_extension() -> Path:
    """The extension as it ships inside the wheel, which is the current build."""
    return Path(__file__).resolve().parent / "extension" / EXTENSION_NAME


def extensions_dir() -> Path:
    """pi's global extension directory, which it discovers without configuration."""
    from . import runtime

    return runtime.home() / "agent" / "extensions"


def installed_extension() -> Path:
    return extensions_dir() / EXTENSION_NAME
