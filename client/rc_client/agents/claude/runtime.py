"""Locate the `claude` executable this device should drive and read its version."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path

from ...channel.paths import path_candidates

VERSION_TIMEOUT = 3.0
_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)")

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"


def candidate_paths() -> list[str]:
    """Resolution order: explicit override, PATH, then the standard install spots."""
    explicit = os.environ.get("RC_CLAUDE_BIN", "").strip()
    candidates: list[str] = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    candidates.extend(path_candidates("claude"))
    home = Path.home()
    candidates.extend(
        str(path)
        for path in (
            home / ".local/bin/claude",
            home / ".claude/local/claude",
            home / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            Path("/opt/homebrew/bin/claude"),
            home / "node_modules/.bin/claude",
            home / ".yarn/bin/claude",
        )
    )
    return candidates


def resolve_binary() -> str | None:
    """The real executable, never the device's own shim.

    The shim only appends channel flags for a person at a terminal, but the
    daemon drives Claude over pipes, so pointing the SDK at the wrapper would
    only add a process to every session.
    """
    from ...channel.shim import is_shim

    seen: set[str] = set()
    for candidate in candidate_paths():
        path = os.path.abspath(os.path.expanduser(candidate))
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if os.path.isfile(path) and os.access(path, os.X_OK) and not is_shim(path):
            return path
    return None


async def probe_version(path: str) -> str | None:
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
