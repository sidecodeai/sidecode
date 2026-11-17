"""Task board settings, read from the gateway's own environment (and so its single ``.env``).

A self-hosted gateway keeps boards on the operator's polish provider (``POLISH_BASE_URL`` and
``POLISH_API_KEY``) with a model of its own, ``BOARD_MODEL``; without all three the feature is off.
No client ever sees or chooses the model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class BoardConfig:
    #: The model a self-hosted gateway's boards are made by; empty keeps the feature off.
    model: str = ""


def load_board_config(environ: Mapping[str, str] | None = None) -> BoardConfig:
    source = os.environ if environ is None else environ
    return BoardConfig(model=(source.get("BOARD_MODEL") or "").strip())
