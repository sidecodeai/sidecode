"""The build this client runs: the SHA-256 of the wheel it was installed from.

`install.sh` records the digest in `$RC_CLIENT_HOME/state/client-build` after a
successful install and `rc-client self-update` rewrites it. A device installed
from a source checkout has no such file and reports `client_build: null`, which
is what tells an app that it cannot be updated remotely (A22).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import state_dir, write_atomic

BUILD_FILENAME = "client-build"
DIGEST = re.compile(r"[0-9a-f]{64}")
READ_CHUNK = 1024 * 1024


def build_path() -> Path:
    return state_dir() / BUILD_FILENAME


def as_digest(value: str) -> str | None:
    """A lower-case SHA-256 hex digest, or None when the text is not one."""
    candidate = value.strip().lower()
    return candidate if DIGEST.fullmatch(candidate) else None


def read_build() -> str | None:
    """The digest recorded at install time, or None when it is absent or unreadable."""
    try:
        text = build_path().read_text(encoding="utf-8")
    except OSError:
        return None
    return as_digest(text)


def write_build(digest: str) -> Path:
    target = build_path()
    write_atomic(target, f"{digest}\n".encode())
    return target


def digest_of(path: Path) -> str:
    """The SHA-256 of a file, read in chunks so a wheel never lands in memory whole."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()
