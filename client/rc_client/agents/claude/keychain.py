"""Claude Code's credential item in the login Keychain, read through the `security` CLI (A33).

`security` is the tool Claude Code writes the item with, so reading it back asks the Keychain for
nothing new. A read comes to one of three things: the item, nothing because the item is not there
(exit 44, `errSecItemNotFound`), or an item that may well be there but cannot be handed over right
now: a locked Keychain, a prompt nobody answered, `security` slow or unable to start. The last is
the one a caller must not mistake for a person who signed out, so it is an exception of its own.
Each outcome but the item is logged by class and exit status; what `security` printed never is.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any

from ...logging_setup import logger

log = logger("rc_client.claude.keychain")

KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT = 5.0
# `security` exits with the low byte of the OSStatus it failed with; -25300 is this one.
NOT_FOUND = 44
# A flag rather than an inline `sys.platform` test, so a type check run on Linux
# still reads the Keychain path below as reachable.
IS_MACOS = sys.platform == "darwin"


class KeychainUnreadable(Exception):
    """The item may be there, but `security` could not hand it over right now."""


async def read_keychain() -> str | None:
    """The Claude Code credential item, as the `security` CLI prints it, or nothing.

    Nothing means there is no Keychain here or no such item. An item that cannot be
    read at this moment raises `KeychainUnreadable` instead.
    """
    if not IS_MACOS:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        raise _unreadable("spawn_failed") from None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), KEYCHAIN_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        raise _unreadable("timeout") from None
    if process.returncode == 0:
        return out.decode("utf-8", "replace").strip() or None
    if process.returncode == NOT_FOUND:
        log.info("claude keychain read", outcome="missing", exit=NOT_FOUND)
        return None
    raise _unreadable("unreadable", process.returncode)


def _unreadable(outcome: str, status: int | None = None) -> KeychainUnreadable:
    seen: dict[str, Any] = {} if status is None else {"exit": status}
    log.warning("claude keychain read", outcome=outcome, **seen)
    return KeychainUnreadable(outcome)
