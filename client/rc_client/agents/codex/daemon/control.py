"""The `codex app-server daemon` subcommands the device runs for itself.

`start` is idempotent and returns in a third of a second, `restart` is the only
thing that replaces a running app-server with a newer build, and `version` is
the only place where the two versions involved are visible at all: the
app-server that is running, and the build the installer left on disk. Each one
prints a single JSON object on its last line.

Every call here goes to the standalone build, because the daemon starts and
updates its app-server from that fixed path and refuses to run from any other.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from ....logging_setup import logger

log = logger("rc_client.codex.daemon.control")

COMMAND_TIMEOUT = 60.0


@dataclass(slots=True)
class DaemonVersion:
    """What `codex app-server daemon version` reports about this machine."""

    status: str
    app_server: str | None
    managed: str | None
    cli: str | None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def drifted(self) -> bool:
        """Whether the app-server that is running is not the build on disk.

        `codex update` replaces the build and leaves the daemon alone, so a
        machine that upgrades keeps serving the old app-server until something
        restarts it. TUIs still join a drifted daemon, so this is never urgent.
        """
        return bool(self.app_server and self.managed and self.app_server != self.managed)


async def run(binary: str, subcommand: str) -> tuple[bool, str]:
    """One `codex app-server daemon <subcommand>`, with its output."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "app-server",
            "daemon",
            subcommand,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT)
    except (OSError, TimeoutError) as exc:
        return False, f"could not run `codex app-server daemon {subcommand}`: {exc}"
    text = out.decode("utf-8", "replace").strip()
    return process.returncode == 0, text[-2000:]


def parse(text: str) -> dict[str, Any]:
    """The JSON object these commands print last, or nothing they printed at all."""
    try:
        parsed = json.loads(text.splitlines()[-1]) if text else {}
    except (json.JSONDecodeError, IndexError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_version(text: str) -> DaemonVersion | None:
    """Parse `daemon version` output. None means it said nothing we understand."""
    parsed = parse(text)
    if not parsed:
        return None
    return DaemonVersion(
        status=str(parsed.get("status") or "unknown"),
        app_server=_text(parsed.get("appServerVersion")),
        managed=_text(parsed.get("managedCodexVersion")),
        cli=_text(parsed.get("cliVersion")),
    )


async def bootstrap(binary: str) -> tuple[bool, str]:
    """`daemon bootstrap`, never with `--remote-control`.

    Remote control enrols the machine with OpenAI's relay, which this project
    does not use.
    """
    ok, text = await run(binary, "bootstrap")
    if not ok:
        return False, text
    return True, str(parse(text).get("status") or "bootstrapped")


async def start(binary: str) -> tuple[bool, str]:
    """`daemon start`: brings the daemon up, or says it was already running."""
    ok, text = await run(binary, "start")
    if not ok:
        return False, text
    return True, str(parse(text).get("status") or "started")


async def restart(binary: str) -> tuple[bool, str]:
    """`daemon restart`: the only command that clears a version drift."""
    ok, text = await run(binary, "restart")
    if not ok:
        return False, text
    return True, str(parse(text).get("status") or "restarted")


async def version(binary: str) -> DaemonVersion | None:
    """`daemon version`, or None when there is no daemon to ask."""
    ok, text = await run(binary, "version")
    if not ok:
        log.debug("codex daemon version failed", error=text[:200])
        return None
    return read_version(text)
