"""Locate the pi executable, read its version, and name its state directory.

pi installs as an ordinary npm binary, so there is no vendor directory to look
in the way Grok and Codex have one: an explicit override, then `PATH`, then the
two prefixes npm uses without `sudo`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from pathlib import Path

# The prefixes npm and Homebrew install into without `sudo`, tried after PATH.
# A test points this at nothing so the machine's own pi is never found.
SYSTEM_PREFIXES = (Path("/opt/homebrew/bin/pi"), Path("/usr/local/bin/pi"))

VERSION_TIMEOUT = 5.0
MODELS_TIMEOUT = 10.0
_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)")


def home() -> Path:
    """pi's state directory. Read on every call so a test can move `$HOME`."""
    return Path.home() / ".pi"


def settings_file() -> Path:
    """Where `/model` and `/thinking` save a person's startup defaults."""
    return home() / "agent" / "settings.json"


def candidate_paths() -> list[str]:
    explicit = os.environ.get("RC_PI_BIN", "").strip()
    candidates: list[str] = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    found = shutil.which("pi")
    if found:
        candidates.append(found)
    candidates.extend(
        str(path)
        for path in (
            Path.home() / ".local/bin/pi",
            Path.home() / ".npm-global/bin/pi",
            *SYSTEM_PREFIXES,
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


async def _run(path: str, *args: str, timeout: float) -> str | None:
    """Everything `pi` prints for a short read-only command, or None."""
    try:
        process = await asyncio.create_subprocess_exec(
            path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        # The child may have exited between the timeout and the signal.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        return None
    return out.decode("utf-8", "replace") + err.decode("utf-8", "replace")


async def probe_version(path: str) -> str | None:
    """`pi --version` prints the bare version, for example `0.85.1`."""
    printed = await _run(path, "--version", timeout=VERSION_TIMEOUT)
    if printed is None:
        return None
    match = _VERSION_RE.search(printed)
    return match.group(1) if match else None


async def probe_models(path: str) -> str | None:
    """`pi --list-models` prints one table row per configured model."""
    return await _run(path, "--list-models", timeout=MODELS_TIMEOUT)
