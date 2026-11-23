"""The settings an `agy` session runs with, as its `configOptions` say, and the thinking-level rule.

`agy` describes a session's settings as three select options: `model`, `thought_level` and `mode`,
the last also reachable through `session/set_mode`. The thinking levels depend on the model: some
offer four, some three, some none, and `agy` puts the level back to the model's own default when
the model changes. In a new process it starts every conversation on the account's defaults, so the
device sets a session's own again each time (A64).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ...models import Choice

MODEL = "model"
THOUGHT_LEVEL = "thought_level"
MODE = "mode"
# Antigravity's thinking levels, weakest first, by `agy`'s own ids and names (4.3).
EFFORT_ORDER = ("low", "medium", "high", "xhigh")
EFFORTS = [
    Choice("low", "Low"),
    Choice("medium", "Medium"),
    Choice("high", "High"),
    Choice("xhigh", "Extra"),
]
# What `permission_modes` says until the device has opened a session (4.3).
FALLBACK_MODES = [
    Choice("default", "Default"),
    Choice("accept-edits", "Accept edits"),
    Choice("auto", "Auto"),
    Choice("plan", "Plan"),
]


@dataclass(slots=True)
class Running:
    """What one `agy` session runs with now.

    `levels` is None when the model has no thinking level at all, which is how `agy` says so: the
    option is missing from the list.
    """

    models: list[Choice] = field(default_factory=list)
    model: str | None = None
    levels: tuple[str, ...] | None = None
    effort: str | None = None
    modes: list[Choice] = field(default_factory=list)
    mode: str | None = None
    # Whether the mode is one of the session options, which `session/set_config_option` sets.
    mode_option: bool = False

    def model_label(self) -> str | None:
        return next((choice.label for choice in self.models if choice.id == self.model), None)


def running(result: dict[str, Any], before: Running | None = None) -> Running:
    """Read a `session/new|load|resume` answer, or a `set_config_option` one.

    An answer without `configOptions` changes nothing; `modes`, the older shape, fills in a mode
    the options do not carry.
    """
    options = result.get("configOptions")
    found = Running() if isinstance(options, list) else _copy(before)
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict):
                _read_option(found, option)
    modes = result.get("modes")
    if isinstance(modes, dict) and not found.mode_option:
        found.modes = _choices(modes.get("availableModes"), "id", "name") or found.modes
        current = modes.get("currentModeId")
        if isinstance(current, str) and current:
            found.mode = current
    return found


def _copy(before: Running | None) -> Running:
    if before is None:
        return Running()
    return Running(
        models=list(before.models),
        model=before.model,
        levels=before.levels,
        effort=before.effort,
        modes=list(before.modes),
        mode=before.mode,
        mode_option=before.mode_option,
    )


def _read_option(found: Running, option: dict[str, Any]) -> None:
    kind = str(option.get("category") or option.get("id") or "")
    current = option.get("currentValue")
    value = current if isinstance(current, str) and current else None
    choices = _choices(_flat(option.get("options")), "value", "name")
    if kind == MODEL:
        found.models = choices
        found.model = value
    elif kind == THOUGHT_LEVEL:
        found.levels = tuple(choice.id for choice in choices)
        found.effort = value
    elif kind == MODE:
        found.modes = choices
        found.mode = value
        found.mode_option = True


def _flat(options: Any) -> list[Any]:
    """A select's options, whether listed flat or in groups of their own."""
    if not isinstance(options, list):
        return []
    flat: list[Any] = []
    for entry in options:
        if isinstance(entry, dict) and isinstance(entry.get("options"), list):
            flat.extend(entry["options"])
        else:
            flat.append(entry)
    return flat


def _choices(entries: Any, key: str, label: str) -> list[Choice]:
    found: list[Choice] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        value = entry.get(key)
        if isinstance(value, str) and value:
            found.append(Choice(value, str(entry.get(label) or value)))
    return found


def clamp(effort: str | None, levels: Sequence[str] | None) -> str | None:
    """The level a session runs at on a model that offers `levels` (4.3).

    The same level while the model offers it, otherwise the nearest one below it — or, when the
    model offers none below, its lowest — and none at all on a model without thinking levels.
    """
    if effort is None or not levels:
        return None
    if effort in levels:
        return effort
    rank = EFFORT_ORDER.index(effort) if effort in EFFORT_ORDER else len(EFFORT_ORDER)
    lower = [
        level for level in levels if level in EFFORT_ORDER and EFFORT_ORDER.index(level) < rank
    ]
    if lower:
        return max(lower, key=EFFORT_ORDER.index)
    return levels[0]
