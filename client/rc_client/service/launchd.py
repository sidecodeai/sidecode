"""launchd integration for macOS (a user agent, never a root daemon)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from ..channel.paths import path_with_shim
from ..config import client_home, log_dir
from ..errors import RcError

LABEL = "dev.remote-control.client"
# `launchctl bootout` returns before launchd has finished tearing the job down,
# and bootstrapping the same label into a domain that is still unloading it
# fails with EIO, leaving the service stopped. Wait it out, then retry.
UNLOAD_TIMEOUT = 15.0
RUNNING_TIMEOUT = 15.0
BOOTSTRAP_ATTEMPTS = 6
POLL_INTERVAL = 0.5
RECOVERY_HINT = "run `rc-client service start` to bring it up"
# No `ProcessType`: `Background` puts the job in the lowest scheduling band and
# throttles its disk I/O, which on a loaded machine stalls the event loop long
# enough for the gateway to call the link dead. Left unset, launchd treats it as
# `Adaptive`, which is what a job that has to answer a person promptly needs.
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{executable}</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>StandardOutPath</key><string>{stdout}</string>
  <key>StandardErrorPath</key><string>{stderr}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{path}</string>
    <key>RC_CLIENT_HOME</key><string>{client_home}</string>
  </dict>
</dict>
</plist>
"""


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def render(executable: str) -> str:
    """Build the plist, escaping every value: paths may contain & or <."""
    return PLIST_TEMPLATE.format(
        label=escape(LABEL),
        executable=escape(executable),
        home=escape(str(Path.home())),
        stdout=escape(str(log_dir() / "rc-client.out.log")),
        stderr=escape(str(log_dir() / "rc-client.err.log")),
        path=escape(path_with_shim(os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))),
        client_home=escape(str(client_home())),
    )


def _loaded() -> bool:
    return _run("launchctl", "print", f"{_domain()}/{LABEL}").returncode == 0


def _running() -> bool:
    result = _run("launchctl", "print", f"{_domain()}/{LABEL}")
    return result.returncode == 0 and "state = running" in result.stdout


def _wait_for(predicate: Callable[[], bool], timeout: float) -> bool:
    """Poll `predicate` until it holds or `timeout` passes, checking it at least twice."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return predicate()
        time.sleep(POLL_INTERVAL)


def _unload() -> None:
    """Boot the job out and wait until launchd has really let go of the label."""
    if not _loaded():
        return
    _run("launchctl", "bootout", f"{_domain()}/{LABEL}")
    _wait_for(lambda: not _loaded(), UNLOAD_TIMEOUT)


def _bootstrap(target: Path) -> None:
    """Load the plist, unloading a previous incarnation and retrying past the race."""
    reason = ""
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        _unload()
        result = _run("launchctl", "bootstrap", _domain(), str(target))
        if result.returncode == 0:
            return
        reason = (result.stderr.strip() or result.stdout.strip())[:200]
        if attempt + 1 < BOOTSTRAP_ATTEMPTS:
            time.sleep(POLL_INTERVAL)
    raise RcError("internal", f"launchctl bootstrap failed: {reason}. {RECOVERY_HINT}")


def install(executable: str | None = None) -> Path:
    """Write the plist and load it, whether or not the service is already running."""
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = executable or _resolve_executable()
    target.write_text(render(binary), encoding="utf-8")
    _bootstrap(target)
    if not _wait_for(_running, RUNNING_TIMEOUT):
        raise RcError(
            "internal",
            f"the service was installed at {target} but is not running: {status()}. "
            f"{RECOVERY_HINT}",
        )
    return target


def uninstall() -> None:
    _run("launchctl", "bootout", f"{_domain()}/{LABEL}")
    plist_path().unlink(missing_ok=True)


def start() -> None:
    """Kickstart the job, bootstrapping it first when `stop` booted it out."""
    target = plist_path()
    if not target.exists():
        raise RcError("not_found", "the service is not installed; run rc-client service install")
    if not _loaded():
        _bootstrap(target)
        return
    result = _run("launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}")
    if result.returncode != 0:
        raise RcError("internal", f"launchctl kickstart failed: {result.stderr.strip()[:200]}")


def stop() -> None:
    _run("launchctl", "bootout", f"{_domain()}/{LABEL}")


def status() -> str:
    if not plist_path().exists():
        return "not installed"
    result = _run("launchctl", "print", f"{_domain()}/{LABEL}")
    if result.returncode != 0:
        return "installed, not loaded"
    for line in result.stdout.splitlines():
        if "state =" in line:
            return line.strip()
    return "loaded"


def _resolve_executable() -> str:
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "rc-client" and candidate.exists():
        return str(candidate)
    guess = Path(sys.executable).parent / "rc-client"
    if guess.exists():
        return str(guess)
    raise RcError("not_found", "cannot locate the rc-client executable to register")
