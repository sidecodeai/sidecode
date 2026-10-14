"""Keep gateway credentials out of agent and tool subprocesses.

The Claude SDK merges `options.env` over `os.environ`, so secrets must be
overwritten with empty *tombstones* rather than deleted from the mapping.
"""

from __future__ import annotations

import os

CONTROL_PLANE_SECRET_KEYS = (
    "RC_DEVICE_TOKEN",
    "RC_PAIR_CODE",
    "RC_GATEWAY_TOKEN",
    "RC_PASSWORD",
)


def child_env_tombstones() -> dict[str, str]:
    return {key: "" for key in CONTROL_PLANE_SECRET_KEYS}


def sanitized_child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in CONTROL_PLANE_SECRET_KEYS:
        env.pop(key, None)
    return env


def scrub_parent_secrets() -> None:
    """Drop credentials the daemon was started with once they are loaded."""
    for key in CONTROL_PLANE_SECRET_KEYS:
        os.environ.pop(key, None)
