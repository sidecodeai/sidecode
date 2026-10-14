"""Codex model catalogue read from `model/list`, cached for 10 minutes."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from ...logging_setup import logger
from ...models import Choice
from .rpc import one_shot

log = logger("rc_client.codex.models")

CACHE_TTL = 600.0
# Builds remembered at once: one live, one just replaced, and room to spare.
MAX_CACHED_BUILDS = 4
EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_LABELS = {
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
    "ultra": "Ultra",
}


# Codex reports a thread with no faster tier as `null` until something sets one
# and as `"default"` once a tier has been cleared; both are the standard speed,
# which `Session.speed` spells as null (amendment A21).
STANDARD_TIERS = frozenset({"default", ""})


def _rank(effort: str, default: int = 0) -> int:
    """Position in `EFFORT_ORDER`; ids Codex adds later sort at `default`."""
    return EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else default


def tier_id(value: Any) -> str | None:
    """One `serviceTier` a thread reports, as a `Session.speed` value."""
    if not isinstance(value, str) or value in STANDARD_TIERS:
        return None
    return value


@dataclass(slots=True)
class ModelCatalog:
    models: list[Choice] = field(default_factory=list)
    default_model: str | None = None
    efforts: list[Choice] = field(default_factory=list)
    default_effort: str | None = None
    per_model_efforts: dict[str, list[str]] = field(default_factory=dict)
    # The service tiers faster than standard, in catalogue order (amendment A21).
    speeds: list[Choice] = field(default_factory=list)
    per_model_speeds: dict[str, list[Choice]] = field(default_factory=dict)

    def speeds_for(self, model: str | None) -> list[Choice]:
        """The tiers this model offers. An unknown model gets the whole union."""
        known = self.per_model_speeds.get(model or self.default_model or "")
        return list(known) if known is not None else list(self.speeds)

    def clamp_effort(self, model: str | None, effort: str | None) -> str | None:
        """Codex does not validate effort at the RPC layer, so clamp it here."""
        if effort is None:
            return None
        supported = self.per_model_efforts.get(model or self.default_model or "")
        if not supported:
            supported = [choice.id for choice in self.efforts]
        if not supported:
            # No catalogue to clamp against: pass the choice through rather than
            # silently dropping what the user picked.
            return effort
        if effort in supported:
            return effort
        wanted = _rank(effort, default=len(EFFORT_ORDER) - 1)
        ranked = sorted(supported, key=_rank)
        lower = [item for item in ranked if _rank(item) <= wanted]
        return lower[-1] if lower else (ranked[0] if ranked else None)


def _service_tiers(raw: Any) -> list[Choice]:
    """The faster tiers one catalogue entry offers, standard ones left out."""
    tiers: list[Choice] = []
    for item in (raw or [])[:16] if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tier = tier_id(item.get("id"))
        if tier is None:
            continue
        tiers.append(Choice(id=tier, label=str(item.get("name") or tier.title())))
    return tiers


def parse_catalog(data: list[dict[str, Any]]) -> ModelCatalog:
    catalog = ModelCatalog()
    seen_efforts: set[str] = set()
    for entry in data[:256]:
        if entry.get("hidden"):
            continue
        model_id = str(entry.get("id") or entry.get("model") or "").strip()
        if not model_id:
            continue
        catalog.models.append(Choice(id=model_id, label=str(entry.get("displayName") or model_id)))
        efforts = [
            str(item.get("reasoningEffort"))
            for item in (entry.get("supportedReasoningEfforts") or [])[:16]
            if item.get("reasoningEffort")
        ]
        catalog.per_model_efforts[model_id] = efforts
        seen_efforts.update(efforts)
        tiers = _service_tiers(entry.get("serviceTiers"))
        catalog.per_model_speeds[model_id] = tiers
        for tier in tiers:
            if tier.id not in {known.id for known in catalog.speeds}:
                catalog.speeds.append(tier)
        if entry.get("isDefault"):
            catalog.default_model = model_id
            default_effort = entry.get("defaultReasoningEffort")
            catalog.default_effort = str(default_effort) if default_effort else None
    if catalog.default_model is None and catalog.models:
        catalog.default_model = catalog.models[0].id
    catalog.efforts = [
        Choice(id=item, label=EFFORT_LABELS.get(item, item.title()))
        for item in EFFORT_ORDER
        if item in seen_efforts
    ]
    return catalog


class CatalogCache:
    """One catalogue per build, because an upgrade is a different catalogue.

    The key is the real path: `packages/standalone/current` is a symlink into a
    per-version release directory, so an upgraded Codex asks under a name of its
    own and the stale answer is never handed to it. A machine sees a couple of
    builds a month, and the entries are small, so the oldest are simply dropped.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, ModelCatalog]] = {}

    async def get(self, binary: str) -> ModelCatalog:
        key = os.path.realpath(binary)
        cached = self._entries.get(key)
        if cached is not None and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        try:
            result = await one_shot(binary, "model/list", {})
            catalog = parse_catalog(list(result.get("data") or []))
        except Exception:
            log.warning("codex model/list failed; keeping the previous catalogue")
            return cached[1] if cached is not None else ModelCatalog()
        self._entries[key] = (time.monotonic(), catalog)
        self._prune()
        return catalog

    def _prune(self) -> None:
        while len(self._entries) > MAX_CACHED_BUILDS:
            oldest = min(self._entries, key=lambda key: self._entries[key][0])
            del self._entries[oldest]


catalog_cache = CatalogCache()
