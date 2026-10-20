"""Which Claude Code runs the `PostToolBatch` hook, by its version (A51).

The terminal proxy reads this before the CLI starts, to hand it a settings
file it can load, so the module is standard library only and is given every
path it reads. A Claude Code that does not know the hook gets the settings file
without it, and steers nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

# The shim's two settings files, and the versions it has seen, side by side.
SETTINGS_NAME = "claude-settings.json"
NO_BATCH_SETTINGS_NAME = "claude-settings-no-batch.json"
VERSIONS_NAME = "claude-versions.json"
# The oldest Claude Code known to run `PostToolBatch` hooks. Neither the public
# changelog nor the hooks reference dates the event, so this is the oldest
# build checked on a machine.
TOOL_BATCH_VERSION = (2, 1, 291)
PROBE_TIMEOUT = 3.0
MAX_REMEMBERED = 32
# A probe that failed is asked again after this long, not kept for good.
FAILED_PROBE_TTL = 300.0
_NUMBERS = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)")


def version_numbers(version: str | None) -> tuple[int, ...] | None:
    """`2.1.303` as `(2, 1, 303)`, or None for a version that cannot be read."""
    match = _NUMBERS.search(version or "")
    return tuple(int(part) for part in match.groups()) if match else None


def runs_tool_batch_hooks(version: str | None) -> bool:
    """Whether a Claude Code of this version runs the `PostToolBatch` hook.

    A version that cannot be read counts as too old, the cautious side: a
    settings file naming an event the CLI does not know may not load at all.
    """
    numbers = version_numbers(version)
    return numbers is not None and numbers >= TOOL_BATCH_VERSION


def binary_version(binary: str, remembered: Path) -> str | None:
    """The version of the CLI at `binary`, asked once per build of it.

    A launch must not cost a `claude --version` every time, so the answer is
    kept in `remembered` by the executable's real path and what tells one
    build of the file from another: size, inode, modification and change
    times, since a package can give every build the same modification time.
    A probe that failed is kept only for `FAILED_PROBE_TTL`.
    """
    try:
        real = os.path.realpath(binary)
        build = _build(os.stat(real))
    except OSError:
        return None
    known = _read(remembered)
    seen = known.get(real)
    if isinstance(seen, dict) and seen.get("build") == build:
        version = seen.get("version")
        if isinstance(version, str):
            return version
        failed = seen.get("failed_at")
        if isinstance(failed, int | float) and time.time() - failed < FAILED_PROBE_TTL:
            return None
    version = _probe(real)
    known.pop(real, None)
    known[real] = {"build": build, "version": version}
    if version is None:
        known[real]["failed_at"] = time.time()
    _write(remembered, dict(list(known.items())[-MAX_REMEMBERED:]))
    return version


def _build(stat: os.stat_result) -> list[float]:
    """What tells one build of an executable from the next one at the same path."""
    return [stat.st_mtime, stat.st_size, stat.st_ino, stat.st_ctime]


def _probe(binary: str) -> str | None:
    try:
        done = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _NUMBERS.search(done.stdout + done.stderr)
    return match.group(0) if match else None


def _read(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write(path: Path, known: dict[str, Any]) -> None:
    """Best effort, all or nothing: two launches at once must not corrupt it."""
    with contextlib.suppress(OSError):
        handle, temporary = tempfile.mkstemp(prefix=".versions-", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(known, out)
            os.replace(temporary, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
