"""The models, modes and defaults `agy` offered the newest session the device opened (A64).

ACP has no call that lists models without a session, and the device reads none of `agy`'s files,
so detection reports what the last session it opened was offered: the `model` option and the values
`agy` started that session on. Until the device has opened one there is nothing to report but the
modes `agy` documents. The record is kept in the device's state, so a restart keeps it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import state_dir, write_atomic
from ...logging_setup import logger
from ...models import Choice
from .options import FALLBACK_MODES, Running

log = logger("rc_client.antigravity.catalog")

STORE_NAME = "antigravity-options.json"
STORE_VERSION = 1


@dataclass(slots=True)
class Catalog:
    models: list[Choice] = field(default_factory=list)
    default_model: str | None = None
    default_effort: str | None = None
    modes: list[Choice] = field(default_factory=lambda: list(FALLBACK_MODES))
    default_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "models": [choice.to_dict() for choice in self.models],
            "default_model": self.default_model,
            "default_effort": self.default_effort,
            "modes": [choice.to_dict() for choice in self.modes],
            "default_mode": self.default_mode,
        }


def store_path() -> Path:
    return state_dir() / STORE_NAME


def load() -> Catalog:
    """What the newest session was offered; the empty catalogue when there was none."""
    try:
        data = json.loads(store_path().read_bytes())
    except (OSError, json.JSONDecodeError):
        return Catalog()
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        return Catalog()
    models = _choices(data.get("models"))
    if not models:
        return Catalog()
    return Catalog(
        models=models,
        default_model=_text(data.get("default_model")),
        default_effort=_text(data.get("default_effort")),
        modes=_choices(data.get("modes")) or list(FALLBACK_MODES),
        default_mode=_text(data.get("default_mode")),
    )


def remember(started: Running) -> bool:
    """Keep what a session just opened was offered; whether that changed what detection says."""
    if not started.models:
        return False
    found = Catalog(
        models=list(started.models),
        default_model=started.model,
        default_effort=started.effort,
        modes=list(started.modes) or list(FALLBACK_MODES),
        default_mode=started.mode,
    )
    if found.to_dict() == load().to_dict():
        return False
    try:
        write_atomic(store_path(), json.dumps(found.to_dict(), ensure_ascii=False).encode("utf-8"))
    except OSError:
        log.warning("could not keep what the newest Antigravity session was offered")
        return False
    return True


def _choices(rows: Any) -> list[Choice]:
    found: list[Choice] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and isinstance(row.get("id"), str) and row["id"]:
            found.append(Choice(str(row["id"]), str(row.get("label") or row["id"])))
    return found


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
