"""Pick the right service backend for this platform."""

from __future__ import annotations

import sys
from pathlib import Path

from ..errors import RcError
from . import launchd, systemd

IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _backend() -> object:
    if IS_MACOS:
        return launchd
    if IS_LINUX:
        return systemd
    raise RcError("unsupported", f"no service integration for {sys.platform}")


def install(executable: str | None = None) -> Path:
    if IS_MACOS:
        return launchd.install(executable)
    return systemd.install(executable)


def uninstall() -> None:
    if IS_MACOS:
        launchd.uninstall()
    else:
        systemd.uninstall()


def start() -> None:
    if IS_MACOS:
        launchd.start()
    else:
        systemd.start()


def install_restarts() -> bool:
    """Whether `install` also puts a running service onto the code it just installed.

    launchd has no way to reload a plist but to boot the job out and back in,
    so its `install` is a restart. systemd's `install` rewrites and enables the
    unit and leaves the running process alone, which is why an update on Linux
    has to ask for the restart itself — the cause of every "did not come back"
    seen on Linux hosts.
    """
    return IS_MACOS


def restart() -> None:
    """Restart a running service, from inside it if need be."""
    if IS_MACOS:
        launchd.start()
    else:
        systemd.restart(block=False)


def stop() -> None:
    if IS_MACOS:
        launchd.stop()
    else:
        systemd.stop()


def status() -> str:
    if IS_MACOS:
        return launchd.status()
    return systemd.status()


def post_install_hint() -> str | None:
    return None if IS_MACOS else systemd.LINGER_HINT
