"""pi's model and thinking-level catalogue.

`pi --list-models` prints one row per configured model, so the models a device
offers are whichever providers the person has logged in to. The thinking levels
are pi's own fixed vocabulary; which of them a model exposes is only knowable
from a running session, so the device advertises the four every reasoning model
has and lets pi decide what a level means for the model in front of it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ...logging_setup import logger
from ...models import Choice
from . import runtime

log = logger("rc_client.pi.catalog")

CACHE_TTL = 600.0

# `--thinking`, weakest first. `minimal`, `xhigh` and `max` exist but only some
# models expose them, so they are ordered and labelled here without being
# advertised unless a person's own default names one.
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
THINKING_LABELS = {
    "off": "Off",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}
ADVERTISED_LEVELS = ("off", "low", "medium", "high")
DEFAULT_LEVEL = "medium"

# pi has no permission system of its own; these three are the device's, enforced
# by the extension it loads into every pi session (A26, PROTOCOL.md 4.3).
PERMISSION_MODES = [
    Choice("untrusted", "Ask for everything"),
    Choice("on-request", "Ask when needed"),
    Choice("never", "Never ask"),
]
DEFAULT_PERMISSION_MODE = "on-request"

# What a device with no provider logged in offers, which is what
# `fixtures/objects/agent.pi.json` says.
FALLBACK_MODELS = [
    Choice("anthropic/claude-sonnet-4-5", "Claude Sonnet 4.5"),
    Choice("openai/gpt-5", "GPT-5"),
]
FALLBACK_DEFAULT_MODEL = "anthropic/claude-sonnet-4-5"

_ACRONYMS = {"gpt": "GPT"}
# The header pi prints above the table, and the width of a version fragment
# that reads as part of the number before it rather than as a date stamp.
_HEADER = ("provider", "model")
_VERSION_PART = 2


@dataclass(slots=True)
class PiCatalog:
    models: list[Choice] = field(default_factory=list)
    default_model: str | None = None
    efforts: list[Choice] = field(default_factory=list)
    default_effort: str | None = None


def label_for(model: str) -> str:
    """`claude-sonnet-4-5` reads as `Claude Sonnet 4.5`, `gpt-5` as `GPT-5`."""
    words: list[str] = []
    for part in model.split("-"):
        if not part:
            continue
        previous = words[-1] if words else ""
        if part[:1].isdigit() and previous:
            # A version fragment continues the token before it: a number takes
            # a dot, an acronym keeps the hyphen. A date stamp is its own word.
            if previous[-1:].isdigit() and len(part) <= _VERSION_PART:
                words[-1] = f"{previous}.{part}"
                continue
            if previous.isupper():
                words[-1] = f"{previous}-{part}"
                continue
        words.append(_ACRONYMS.get(part.lower(), part[:1].upper() + part[1:]))
    return " ".join(words) or model


def parse_models(printed: str) -> list[Choice]:
    """The `provider  model  …` table, as the `provider/model` ids pi accepts.

    pi prints a sentence instead of a table when no provider is logged in, so
    nothing is read until the header row says a table has started.
    """
    models: list[Choice] = []
    started = False
    for line in printed.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        if not started:
            started = (fields[0], fields[1]) == _HEADER
            continue
        models.append(Choice(f"{fields[0]}/{fields[1]}", label_for(fields[1])))
    return models


def _read_settings() -> dict[str, Any]:
    try:
        data = json.loads(runtime.settings_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _efforts(extra: str | None) -> list[Choice]:
    """The advertised levels, plus one a person chose that is not among them."""
    ids = list(ADVERTISED_LEVELS)
    if extra and extra in THINKING_LEVELS and extra not in ids:
        ids.append(extra)
    ids.sort(key=THINKING_LEVELS.index)
    return [Choice(level, THINKING_LABELS[level]) for level in ids]


def _apply_settings(catalog: PiCatalog, settings: dict[str, Any]) -> None:
    """`~/.pi/agent/settings.json` holds the defaults `/model` and `/thinking` save."""
    provider = settings.get("defaultProvider")
    model = settings.get("defaultModel")
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        catalog.default_model = f"{provider}/{model}"
    level = settings.get("defaultThinkingLevel")
    chosen = level if isinstance(level, str) and level in THINKING_LEVELS else None
    catalog.default_effort = chosen or DEFAULT_LEVEL
    catalog.efforts = _efforts(chosen)


class ModelCache:
    """`pi --list-models` costs a Node start, so it is read at most every 10 min."""

    def __init__(self) -> None:
        self._value: list[Choice] | None = None
        self._fetched_at = 0.0

    async def get(self, binary: str) -> list[Choice]:
        if self._value is not None and time.monotonic() - self._fetched_at < CACHE_TTL:
            return self._value
        printed = await runtime.probe_models(binary)
        if printed is None:
            log.warning("pi --list-models did not answer; keeping the previous catalogue")
            return self._value or []
        self._value = parse_models(printed)
        self._fetched_at = time.monotonic()
        return self._value

    def forget(self) -> None:
        self._value = None
        self._fetched_at = 0.0


model_cache = ModelCache()


async def load(binary: str | None) -> PiCatalog:
    """Everything pi advertises, or the shipped defaults when nothing is logged in."""
    models = await model_cache.get(binary) if binary else []
    catalog = PiCatalog(models=list(models) if models else list(FALLBACK_MODELS))
    catalog.default_model = catalog.models[0].id if models else FALLBACK_DEFAULT_MODEL
    _apply_settings(catalog, _read_settings())
    return catalog


def context_window(model: dict[str, Any] | None) -> int | None:
    """The window of the `Model` object `get_state` reports, when it has one."""
    window = (model or {}).get("contextWindow")
    return window if isinstance(window, int) and window > 0 else None


def model_id(model: dict[str, Any] | None) -> str | None:
    """`provider/id` for the `Model` object pi reports, or None before login."""
    provider = str((model or {}).get("provider") or "")
    identifier = str((model or {}).get("id") or "")
    if not provider or not identifier or "unknown" in (provider, identifier):
        return None
    return f"{provider}/{identifier}"


def split_model(model: str) -> tuple[str, str]:
    """`set_model` takes the provider and the id apart, the way pi stores them."""
    provider, _, identifier = model.partition("/")
    return (provider, identifier) if identifier else ("", provider)
