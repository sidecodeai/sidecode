"""Locate `agy`, read its version, and name the empty folder of the device's own it lists from.

The device runs nothing of Antigravity's but the `agy` executable, and reads none of its files:
no part of `~/.gemini` is opened, listed or locked here or anywhere in this package (A64).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from pathlib import Path

from ...config import state_dir

VERSION_TIMEOUT = 3.0
# The oldest `agy` whose `acp` the device drives: earlier ones lack the fixes the turn,
# question and list handling here rely on (A64).
MIN_VERSION = (1, 5, 13)
_VERSION = re.compile(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)")


def candidate_paths() -> list[str]:
    """`RC_ANTIGRAVITY_BIN`, then `agy` on `PATH`, then `~/.local/bin/agy`, where it installs."""
    candidates: list[str] = []
    explicit = os.environ.get("RC_ANTIGRAVITY_BIN", "").strip()
    if explicit:
        candidates.append(os.path.expanduser(explicit))
    found = shutil.which("agy")
    if found:
        candidates.append(found)
    candidates.append(str(Path.home() / ".local" / "bin" / "agy"))
    return candidates


def resolve_binary() -> str | None:
    for candidate in candidate_paths():
        path = os.path.abspath(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


async def probe_version(path: str) -> str | None:
    """`agy --version`, read within three seconds; None when it says nothing readable."""
    try:
        process = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return None
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=VERSION_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        return None
    found = _VERSION.search(out.decode("utf-8", "replace") + err.decode("utf-8", "replace"))
    return found.group(0) if found else None


def parse_version(version: str | None) -> tuple[int, int, int] | None:
    found = _VERSION.search(version or "")
    if found is None:
        return None
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def too_old(version: str | None) -> bool:
    """Whether `version` is older than 1.5.13. A version that cannot be read is not refused here."""
    parsed = parse_version(version)
    return parsed is not None and parsed < MIN_VERSION


def lists_dir() -> Path:
    """The empty folder the device's list process and usage query run in (A64).

    `agy` reports a conversation it kept without a folder under the folder of the process that
    lists it, so the listing runs where no project is, and such a conversation is told apart by
    this folder. It is the device's own, under its state, and stays empty.
    """
    folder = state_dir() / "antigravity" / "lists"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder
