"""Supervision for the shared Codex app-server daemon.

`codex app-server daemon bootstrap` starts the daemon with a plain pid backend:
no launchd job, no systemd unit, and nothing that survives a reboot. Codex's
own `daemon start` is idempotent and returns immediately, so supervision here is
a nudge repeated every few minutes rather than a process to keep alive.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from ..config import log_dir
from ..errors import RcError

LABEL = "dev.remote-control.codex-daemon"
UNIT = "rc-codex-daemon.service"
NUDGE_SECONDS = 300

# No `ProcessType`, for the same reason as the device's own agent: `Background`
# is the lowest scheduling band and throttled disk I/O, and this job is the
# app-server every Codex round trip goes through.
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{executable}</string>
    <string>app-server</string>
    <string>daemon</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>StandardOutPath</key><string>{stdout}</string>
  <key>StandardErrorPath</key><string>{stderr}</string>
</dict>
</plist>
"""

UNIT_TEMPLATE = """[Unit]
Description=Shared Codex app-server daemon for remote-control
After=default.target

[Service]
Type=simple
ExecStart={executable} app-server daemon start
Restart=always
RestartSec={interval}

[Install]
WantedBy=default.target
"""

LINGER_HINT = "Run `loginctl enable-linger $USER` so the Codex daemon survives your session ending."

IS_MACOS = sys.platform == "darwin"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd/user" / UNIT


def render_plist(executable: str) -> str:
    return PLIST_TEMPLATE.format(
        label=escape(LABEL),
        executable=escape(executable),
        interval=NUDGE_SECONDS,
        stdout=escape(str(log_dir() / "codex-daemon.out.log")),
        stderr=escape(str(log_dir() / "codex-daemon.err.log")),
    )


def render_unit(executable: str) -> str:
    return UNIT_TEMPLATE.format(executable=executable, interval=NUDGE_SECONDS)


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(executable: str) -> Path:
    """Write and load our supervision. Idempotent: a second call rewrites it."""
    if IS_MACOS:
        target = plist_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        log_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(render_plist(executable), encoding="utf-8")
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        result = _launchctl("bootstrap", _domain(), str(target))
        if result.returncode != 0:
            reason = (result.stderr.strip() or result.stdout.strip())[:200]
            raise RcError("internal", f"launchctl bootstrap failed: {reason}")
        return target
    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_unit(executable), encoding="utf-8")
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", UNIT)
    if result.returncode != 0:
        raise RcError("internal", f"systemctl enable failed: {result.stderr.strip()[:200]}")
    return target


def uninstall() -> None:
    """Remove our supervision only. Codex itself and its daemon stay put."""
    if IS_MACOS:
        _launchctl("bootout", f"{_domain()}/{LABEL}")
        plist_path().unlink(missing_ok=True)
        return
    _systemctl("disable", "--now", UNIT)
    unit_path().unlink(missing_ok=True)
    _systemctl("daemon-reload")


def status() -> str:
    if IS_MACOS:
        if not plist_path().exists():
            return "not installed"
        loaded = _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
        return "loaded" if loaded else "installed, not loaded"
    if not unit_path().exists():
        return "not installed"
    return _systemctl("is-enabled", UNIT).stdout.strip() or "unknown"


def post_install_hint() -> str | None:
    return None if IS_MACOS else LINGER_HINT
