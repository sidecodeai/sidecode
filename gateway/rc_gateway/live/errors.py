"""One error type for everything the live assistant refuses or fails at."""

from __future__ import annotations

import re
from typing import Any

_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class LiveError(Exception):
    """``code`` is the machine-readable reason a call refuses, fails or ends for."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def safe_code(value: Any) -> str:
    """An identifier the Live API gave, only if it looks like one: never free text."""
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else "unknown"
