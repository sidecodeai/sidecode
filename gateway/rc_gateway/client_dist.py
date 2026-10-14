"""The client wheel this gateway serves.

One place resolves ``rc_client-latest.whl``, so the file ``/dist/rc_client-latest.whl`` hands out
and the build ``GET /api/config`` advertises can never disagree. A device compares that build with
its own and refuses any wheel whose SHA-256 is not the one the app asked for (A22).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

#: The stable alias, and the URL the apps hand to the device in ``device.update``.
WHEEL_ALIAS = "rc_client-latest.whl"
WHEEL_URL = f"/dist/{WHEEL_ALIAS}"
WHEEL_GLOB = "rc_client-*.whl"
_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ServedClient:
    """What ``GET /api/config`` reports as ``client``."""

    version: str
    build: str
    url: str = WHEEL_URL


def newest_wheel(directory: Path) -> Path | None:
    """The wheel ``rc_client-latest.whl`` resolves to, or None when the image carries none."""
    if not directory.is_dir():
        return None
    wheels = sorted(
        (item for item in directory.glob(WHEEL_GLOB) if item.is_file()),
        key=lambda item: item.stat().st_mtime,
    )
    return wheels[-1] if wheels else None


def served_client(directory: Path) -> ServedClient | None:
    """Describe the served wheel. None in a source checkout that has never built one."""
    wheel = newest_wheel(directory)
    if wheel is None:
        return None
    try:
        build = _digest(wheel)
    except OSError:
        return None
    return ServedClient(version=wheel_version(wheel.name), build=build)


def wheel_version(filename: str) -> str:
    """``rc_client-0.1.0-py3-none-any.whl`` names version ``0.1.0`` (PEP 427 field order)."""
    parts = filename.split("-")
    return parts[1] if len(parts) > 1 and parts[1] else "unknown"


def _digest(wheel: Path) -> str:
    digest = hashlib.sha256()
    with wheel.open("rb") as stream:
        while chunk := stream.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
