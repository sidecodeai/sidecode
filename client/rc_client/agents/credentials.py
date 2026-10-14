"""Reading an agent's own credential files, and never writing one (A33).

A credential file that is missing, unreadable or malformed means the agent is
signed in nowhere. It is never an exception out of detection, and its contents
are never logged: only the few fields each agent's account module names ever
leave these functions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def table(path: Path) -> dict[str, Any]:
    """The JSON object at `path`, or an empty one when there is none to read."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse(raw)


def parse(raw: str | None) -> dict[str, Any]:
    """The JSON object in `raw`, or an empty one when it is not one."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def word(value: Any) -> str | None:
    """A vendor's own lowercase word, or nothing when it recorded none."""
    return value.strip().lower() or None if isinstance(value, str) else None


def text(value: Any) -> str | None:
    """A vendor's string as it recorded it, or nothing when it is not one."""
    return value.strip() or None if isinstance(value, str) else None


def endpoint(url: Any, vendor: str) -> str | None:
    """The host a key is sent to, when it is not `vendor`'s own (A33).

    Host only: the protocol takes no path here, and a base URL is the one place
    a person's own relay could carry a token in its query string.
    """
    raw = text(url)
    if raw is None:
        return None
    name = urlparse(raw if "//" in raw else f"//{raw}").hostname
    return name if name and name != vendor else None
