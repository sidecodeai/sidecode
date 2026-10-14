"""Keeping the shared Codex daemon running, from inside the running device.

A Codex TUI never starts the shared daemon itself: it runs its own app-server
embedded and joins the shared one only if it is already there. So a machine
whose daemon is not running has every terminal Codex on takeover-only for ever,
and nothing the person does in the terminal will ever change that. The device
is the only thing in a position to notice, and `daemon start` is a third of a
second and idempotent, so it notices on the mirror's scan interval.

Two repairs live here, and neither ever downloads anything: installing Codex
stays in `rc-client codex setup`, where a person asked for it.

- The daemon is not running and the standalone build is there: start it, and
  install our supervision if the machine has none.
- The daemon is running an older app-server than the build on disk, which is
  what `codex update` leaves behind: restart it, but only when nobody is in it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from ....logging_setup import logger
from ....service import codex as supervision
from . import control, setup
from .transport import socket_overridden

log = logger("rc_client.codex.daemon.repair")

SafetyCheck = Callable[[], Awaitable[bool]]

# `daemon start` is cheap, but a machine where it keeps failing is a machine
# where something is wrong that repeating the call will not fix.
START_INTERVAL = 60.0
BACKOFF_INTERVAL = 600.0
FAILURE_LIMIT = 3
# Drift costs nothing while it lasts — TUIs join a drifted daemon perfectly
# well — so this only has to be often enough to catch an upgrade the same day.
DRIFT_INTERVAL = 900.0

NEVER = 0.0


class DaemonRepair:
    """The device's own repairs to the shared daemon, rate-limited and quiet."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures = 0
        self._last_start = NEVER
        self._last_drift = NEVER
        # Supervision is installed at most once per device start: the person may
        # have removed it deliberately since, and a device that rewrites it on
        # every scan would put it straight back.
        self._supervised = False
        self._reported = ""
        self._drift_logged = False

    # ------------------------------------------------------------ the daemon

    async def ensure_running(self) -> bool:
        """Start the daemon when the socket is not answering. True if it is up now."""
        binary = self._binary()
        if binary is None:
            return False
        now = self._clock()
        if not self._start_due(now):
            return False
        self._last_start = now
        ok, detail = await control.start(binary)
        if not ok:
            self._failures += 1
            self._report(f"could not start the shared codex daemon: {detail}")
            return False
        self._failures = 0
        self._reported = ""
        log.info("started the shared codex daemon", status=detail)
        await self._supervise(binary)
        return True

    def _start_due(self, now: float) -> bool:
        if self._last_start == NEVER:
            return True
        interval = BACKOFF_INTERVAL if self._failures >= FAILURE_LIMIT else START_INTERVAL
        return now - self._last_start >= interval

    async def _supervise(self, binary: str) -> None:
        """Install the nudge that `codex setup` installs, if this machine has none.

        A device enrolled before the daemon existed, or one whose setup step
        failed, has nothing keeping the daemon up across a reboot. Both calls
        shell out, so they run off the event loop.
        """
        if self._supervised:
            return
        self._supervised = True
        try:
            if await asyncio.to_thread(supervision.status) != "not installed":
                return
            target = await asyncio.to_thread(supervision.install, binary)
        except Exception as exc:
            log.warning("could not supervise the shared codex daemon", error=str(exc)[:200])
            return
        log.info("installed supervision for the shared codex daemon", path=str(target))

    # ------------------------------------------------------------ the drift

    async def check_drift(self, safe: SafetyCheck) -> bool:
        """Restart a daemon older than the build on disk. True if it was restarted.

        A restart drops every subscriber the daemon has, including the terminals
        it is serving, so it waits until nobody is in one. Waiting is free: the
        old app-server keeps working, it is only out of date.
        """
        binary = self._binary()
        if binary is None:
            return False
        now = self._clock()
        if self._last_drift != NEVER and now - self._last_drift < DRIFT_INTERVAL:
            return False
        self._last_drift = now
        found = await control.version(binary)
        if found is None or not found.drifted:
            self._drift_logged = False
            return False
        if not await safe():
            if not self._drift_logged:
                self._drift_logged = True
                log.info(
                    "the shared codex daemon is out of date; waiting for it to go quiet",
                    running=found.app_server,
                    installed=found.managed,
                )
            return False
        ok, detail = await control.restart(binary)
        if not ok:
            self._report(f"could not restart the shared codex daemon: {detail}")
            return False
        self._drift_logged = False
        log.info(
            "restarted the shared codex daemon onto the installed build",
            installed=found.managed,
            was=found.app_server,
        )
        return True

    # ------------------------------------------------------------- internals

    def _binary(self) -> str | None:
        """The build to command, or None when the device must not touch anything."""
        if socket_overridden():
            return None
        return setup.standalone_binary()

    def _report(self, message: str) -> None:
        """Log a failure once, rather than once a minute for as long as it lasts."""
        if message == self._reported:
            return
        self._reported = message
        log.warning(message)
