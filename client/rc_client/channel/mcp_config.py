"""The `--mcp-config` file the shim hands to Claude Code.

Written by both `rc-client shim install` and the daemon at startup, so the file
matches whichever of the two ran most recently and always names an executable
that exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import client_home, write_atomic
from . import paths


def mcp_config(command: list[str] | None = None) -> dict[str, Any]:
    argv = command or paths.channel_command()
    return {
        "mcpServers": {
            paths.CHANNEL_SERVER_NAME: {
                "command": argv[0],
                "args": argv[1:],
                "env": {"RC_CLIENT_HOME": str(client_home())},
            }
        }
    }


def write_mcp_config(command: list[str] | None = None) -> Path:
    target = paths.mcp_config_path()
    payload = json.dumps(mcp_config(command), indent=2) + "\n"
    write_atomic(target, payload.encode("utf-8"), mode=0o600)
    return target
