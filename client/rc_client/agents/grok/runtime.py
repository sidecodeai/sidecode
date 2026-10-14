"""Locate the Grok Build executable, read its version, and name its home."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from pathlib import Path

VERSION_TIMEOUT = 3.0
_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)")


def home() -> Path:
    """Grok's state directory. Read on every call so a test can move `$HOME`.

    Grok honours `GROK_HOME`, so the device does too: it is what an isolated
    check against the real binary runs under, and what a person who keeps their
    Grok state elsewhere expects to be read.
    """
    override = os.environ.get("GROK_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".grok"


def sessions_dir() -> Path:
    return home() / "sessions"


def models_cache() -> Path:
    return home() / "models_cache.json"


def config_file() -> Path:
    return home() / "config.toml"


def active_sessions_file() -> Path:
    return home() / "active_sessions.json"


def candidate_paths() -> list[str]:
    """Resolution order: explicit override, Grok's own install, then PATH.

    The shipped launcher is `~/.grok/bin/agent`; `grok` is often a shell
    function wrapping it with `--yolo`, which a device must never inherit.
    `shutil.which` only ever finds executables, so a function cannot be picked
    up here, and Grok's own install comes first in any case.
    """
    explicit = os.environ.get("RC_GROK_BIN", "").strip()
    candidates: list[str] = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    candidates.append(str(home() / "bin/agent"))
    for name in ("grok", "agent"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(
        str(path)
        for path in (
            Path.home() / ".local/bin/grok",
            Path("/opt/homebrew/bin/grok"),
            Path("/usr/local/bin/grok"),
        )
    )
    return candidates


def resolve_binary() -> str | None:
    seen: set[str] = set()
    for candidate in candidate_paths():
        path = os.path.abspath(os.path.expanduser(candidate))
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


async def probe_version(path: str) -> str | None:
    """`agent --version` prints `grok <version> (<commit>)`."""
    try:
        process = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=VERSION_TIMEOUT)
    except TimeoutError:
        # The child may have exited between the timeout and the signal.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        return None
    match = _VERSION_RE.search(out.decode("utf-8", "replace") + err.decode("utf-8", "replace"))
    return match.group(1) if match else None
