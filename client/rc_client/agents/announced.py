"""The agents the device last detected, kept for the first hello of its next run.

Detection asks every agent's CLI what it offers, and on a loaded machine that
took tens of seconds, so the link no longer waits for it (`Daemon.run`). The
gateway replaces a device's agents with whatever its `hello` lists, so a hello
sent before this run's detection says what the last run found, and
`agents.updated` follows once this run knows. A device that never detected
anything says it has no agents, which is what it would have said before.
"""

from __future__ import annotations

import json
from typing import Any

from ..registry import Registry

KEY = "agents.announced"


def load(registry: Registry) -> list[dict[str, Any]]:
    raw = registry.get_kv(KEY)
    try:
        listed = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(listed, list):
        return []
    return [item for item in listed if isinstance(item, dict)]


def save(registry: Registry, listed: list[dict[str, Any]]) -> None:
    registry.set_kv(KEY, json.dumps(listed, ensure_ascii=False))
