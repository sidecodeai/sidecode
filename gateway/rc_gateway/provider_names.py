"""Who each AI feature sends data to, by the name the apps show (A65).

The operator names each provider in the root ``.env``: ``STT_PROVIDER_NAME``,
``POLISH_PROVIDER_NAME``, ``BOARD_PROVIDER_NAME`` and ``LIVE_PROVIDER_NAME``. ``GET /api/config``
and the app ``hello`` report the first three as ``provider_name`` in ``stt``, ``polish`` and
``board``; ``GET /api/live/status`` reports the fourth. An app puts the name into what it shows
before the feature sends anything, so a name that is not set is left out rather than guessed.

A gateway with a hosted service offers no AI feature whose provider has no name, and logs the
variable it is missing; a self-hosted gateway may leave every name unset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .logging import logger

log = logger("rc_gateway.provider_names")

#: The longest name an app has to fit into a sentence.
MAX_CHARS = 40

STT = "stt"
POLISH = "polish"
BOARD = "board"
LIVE = "live"

#: Each feature's variable, in the order the configuration lists them.
VARIABLES: dict[str, str] = {
    STT: "STT_PROVIDER_NAME",
    POLISH: "POLISH_PROVIDER_NAME",
    BOARD: "BOARD_PROVIDER_NAME",
    LIVE: "LIVE_PROVIDER_NAME",
}


@dataclass(frozen=True)
class ProviderNames:
    """The name of each AI feature's provider; empty while the operator has named none."""

    stt: str = ""
    polish: str = ""
    board: str = ""
    live: str = ""


def parse(variable: str, raw: str) -> str:
    """The trimmed name, empty when unset; ``ValueError`` for one an app could not show as is.

    A name is plain text: printable characters only, so no control, format or line-breaking
    character can reorder or hide part of the sentence it is shown in.
    """
    name = raw.strip()
    if len(name) > MAX_CHARS:
        raise ValueError(f"{variable} is longer than {MAX_CHARS} characters")
    if not name.isprintable():
        raise ValueError(f"{variable} must be plain text, without control characters")
    return name


def load(read: Callable[[str], str]) -> ProviderNames:
    """Every name, ``read`` giving a variable's raw value ("" when it is not set)."""
    values = {feature: parse(variable, read(variable)) for feature, variable in VARIABLES.items()}
    return ProviderNames(**values)


def named(view: dict[str, Any], name: str) -> dict[str, Any]:
    """``view`` with the provider's name in ``provider_name``, or as it is while it has none."""
    return {**view, "provider_name": name} if name else view


def warn_unnamed(feature: str) -> None:
    """Say which variable a hosted gateway is missing for a feature it therefore keeps off."""
    log.warning(
        "feature off: its provider has no name", feature=feature, missing=VARIABLES[feature]
    )
