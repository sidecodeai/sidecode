"""systemd user-unit integration for Linux."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..channel.paths import path_with_shim
from ..config import client_home
from ..errors import RcError

UNIT = "rc-client.service"
UNIT_TEMPLATE = """[Unit]
Description=remote-control device daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={executable} run
Restart=always
RestartSec=5
Environment=RC_CLIENT_HOME={client_home}
Environment=PATH={path}
WorkingDirectory={home}
NoNewPrivileges=yes
UMask=0077

[Install]
WantedBy=default.target
"""
LINGER_HINT = (
    "Run `loginctl enable-linger $USER` so the daemon keeps running when you are logged out."
)


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd/user" / UNIT


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def render(executable: str) -> str:
    return UNIT_TEMPLATE.format(
        executable=executable,
        home=str(Path.home()),
        client_home=str(client_home()),
        path=path_with_shim(os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")),
    )


def install(executable: str | None = None) -> Path:
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(executable or _resolve_executable()), encoding="utf-8")
    _run("daemon-reload")
    result = _run("enable", UNIT)
    if result.returncode != 0:
        raise RcError("internal", f"systemctl enable failed: {result.stderr.strip()[:200]}")
    return target


def uninstall() -> None:
    _run("disable", "--now", UNIT)
    unit_path().unlink(missing_ok=True)
    _run("daemon-reload")


def start() -> None:
    restart()


def restart(block: bool = True) -> None:
    """Restart the unit, waiting for it unless the caller is inside it.

    `install` only rewrites and enables the unit; the process that is running
    keeps the code it started with until the unit is restarted. The updater
    runs inside the unit's own cgroup, so a restart it asks for kills it too:
    it queues the job with `--no-block` and exits before systemd gets to it.
    """
    arguments = ["restart", UNIT] if block else ["--no-block", "restart", UNIT]
    result = _run(*arguments)
    if result.returncode != 0:
        raise RcError("internal", f"systemctl restart failed: {result.stderr.strip()[:200]}")


def stop() -> None:
    _run("stop", UNIT)


def status() -> str:
    if not unit_path().exists():
        return "not installed"
    result = _run("is-active", UNIT)
    return result.stdout.strip() or "unknown"


def _resolve_executable() -> str:
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "rc-client" and candidate.exists():
        return str(candidate)
    guess = Path(sys.executable).parent / "rc-client"
    if guess.exists():
        return str(guess)
    raise RcError("not_found", "cannot locate the rc-client executable to register")
