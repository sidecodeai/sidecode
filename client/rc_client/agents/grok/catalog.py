"""Grok's model and effort catalogue, read from the files the CLI keeps.

`~/.grok/models_cache.json` is the CLI's own copy of the model list it fetched
from xAI, and `~/.grok/config.toml` holds the defaults a person chose. Reading
both costs nothing, so detection never starts an agent process; the same
catalogue is what clamps an effort to the model that is actually running.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...models import Choice
from . import runtime

# The union order apps show efforts in, weakest first.
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh")
EFFORT_LABELS = {
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
}
# What a device with no cache file offers, which is what `agent.grok.json` says.
FALLBACK_MODELS = [Choice("grok-4.6", "Grok 4.6"), Choice("grok-4.5", "Grok 4.5")]
FALLBACK_EFFORTS = ["low", "medium", "high", "xhigh"]
FALLBACK_DEFAULT_MODEL = "grok-4.6"
FALLBACK_DEFAULT_EFFORT = "high"


@dataclass(slots=True)
class GrokCatalog:
    models: list[Choice] = field(default_factory=list)
    default_model: str | None = None
    efforts: list[Choice] = field(default_factory=list)
    default_effort: str | None = None
    # Per model: the effort ids that model accepts, in `EFFORT_ORDER`.
    efforts_by_model: dict[str, list[str]] = field(default_factory=dict)
    context_windows: dict[str, int] = field(default_factory=dict)

    def clamp_effort(self, model: str | None, effort: str | None) -> str | None:
        """Drop an effort the model does not offer, as Grok itself would."""
        if effort is None:
            return None
        allowed = self.efforts_by_model.get(model or self.default_model or "")
        if allowed and effort not in allowed:
            return None
        return effort

    def context_window(self, model: str | None) -> int | None:
        return self.context_windows.get(model or self.default_model or "")


def _sorted_efforts(ids: set[str]) -> list[str]:
    known = [effort for effort in EFFORT_ORDER if effort in ids]
    return known + sorted(ids - set(known))


def _label(effort: str) -> str:
    return EFFORT_LABELS.get(effort, effort[:1].upper() + effort[1:])


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _fallback() -> GrokCatalog:
    return GrokCatalog(
        models=list(FALLBACK_MODELS),
        default_model=FALLBACK_DEFAULT_MODEL,
        efforts=[Choice(effort, _label(effort)) for effort in FALLBACK_EFFORTS],
        default_effort=FALLBACK_DEFAULT_EFFORT,
        efforts_by_model={choice.id: list(FALLBACK_EFFORTS) for choice in FALLBACK_MODELS},
    )


def _model_entries(cache: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    models = cache.get("models")
    if not isinstance(models, dict):
        return []
    entries: list[tuple[str, dict[str, Any]]] = []
    for model_id, entry in models.items():
        info = entry.get("info") if isinstance(entry, dict) else None
        if not isinstance(info, dict) or info.get("hidden"):
            continue
        entries.append((str(model_id), info))
    return entries


def load() -> GrokCatalog:
    """Everything Grok advertises, or the shipped defaults when nothing is cached."""
    entries = _model_entries(_read_json(runtime.models_cache()))
    if not entries:
        catalog = _fallback()
    else:
        catalog = GrokCatalog()
        union: set[str] = set()
        for model_id, info in entries:
            catalog.models.append(Choice(model_id, str(info.get("name") or model_id)))
            allowed = _model_efforts(info)
            catalog.efforts_by_model[model_id] = allowed
            union.update(allowed)
            window = info.get("context_window")
            if isinstance(window, int):
                catalog.context_windows[model_id] = window
        catalog.efforts = [Choice(effort, _label(effort)) for effort in _sorted_efforts(union)]
        catalog.default_model = catalog.models[0].id if catalog.models else None
        catalog.default_effort = _catalogue_default_effort(entries)
    _apply_config(catalog, _read_config(runtime.config_file()))
    return catalog


def _model_efforts(info: dict[str, Any]) -> list[str]:
    efforts = info.get("reasoning_efforts")
    if not info.get("supports_reasoning_effort") or not isinstance(efforts, list):
        return []
    ids = {
        str(entry.get("id") or entry.get("value") or "")
        for entry in efforts
        if isinstance(entry, dict)
    }
    return _sorted_efforts({effort for effort in ids if effort})


def _catalogue_default_effort(entries: list[tuple[str, dict[str, Any]]]) -> str | None:
    """The effort the first model marks as its own default."""
    _, info = entries[0]
    efforts = info.get("reasoning_efforts")
    if isinstance(efforts, list):
        for entry in efforts:
            if isinstance(entry, dict) and entry.get("default"):
                return str(entry.get("id") or entry.get("value") or "") or None
    chosen = info.get("reasoning_effort")
    return str(chosen) if isinstance(chosen, str) and chosen else None


def _apply_config(catalog: GrokCatalog, config: dict[str, Any]) -> None:
    """`[models] default` and `default_reasoning_effort` are what a person chose."""
    models = config.get("models")
    if not isinstance(models, dict):
        return
    chosen = models.get("default")
    if isinstance(chosen, str) and chosen:
        catalog.default_model = chosen
    effort = models.get("default_reasoning_effort")
    if isinstance(effort, str) and effort:
        catalog.default_effort = effort
