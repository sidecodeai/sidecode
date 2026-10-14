"""`rc-client codex setup` and `rc-client codex status`.

Three steps, each safe to repeat: make sure the standalone Codex exists, ask it
to bootstrap its shared daemon, and register our own supervision because the
bootstrap leaves none. The one thing this never does is enable remote control:
that enrols the machine with OpenAI's relay, which is not what this project is.

"Codex exists" means the standalone build specifically. The daemon commands
refuse to run without it whichever build invokes it, so an npm or Homebrew
`codex` earlier on PATH must never be mistaken for one, and is only ever
reported. Nothing here removes another Codex install.

This is also the only place that downloads anything. The running device repairs
a daemon that is not up (`daemon/repair.py`), but installing Codex is a thing a
person asks for.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .... import __version__
from ....config import state_dir
from ....service import codex as supervision
from ..runtime import CODEX_HOME, STANDALONE
from . import control
from .rpc import handshake_ok
from .transport import socket_exists, socket_path

# Where the official installer puts its `codex` symlink.
LOCAL_BIN = Path(os.environ.get("CODEX_INSTALL_DIR") or (Path.home() / ".local/bin"))
INSTALL_URL = "https://chatgpt.com/codex/install.sh"
DOWNLOAD_TIMEOUT = 60.0
MAX_INSTALLER_BYTES = 1024 * 1024

MISSING = "The standalone Codex install is missing"
PATH_HINT = f'Codex is not on your PATH. Add it with: export PATH="{LOCAL_BIN}:$PATH"'


def path_entries(path_value: str | None = None) -> list[str]:
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    return [os.path.abspath(os.path.expanduser(item)) for item in raw.split(os.pathsep) if item]


def on_path(directory: Path, path_value: str | None = None) -> bool:
    return str(directory.expanduser().resolve()) in path_entries(path_value)


def installer_home() -> Path:
    """A throwaway `HOME` to run the official installer under.

    That installer rewrites a shell profile whenever another `codex` is on PATH,
    whatever else is true, and it reads the standalone build itself as an
    npm-managed one — so on a machine that already has Codex it always rewrites.
    `HOME` is the only thing that decides which file it writes, and the dotfiles
    here are symlinks a rewrite would replace with a regular file. Pointing
    `HOME` at a directory of our own confines the write to a file nobody reads,
    while `CODEX_HOME` and `CODEX_INSTALL_DIR` keep the install itself exactly
    where it belongs.
    """
    return state_dir() / "codex-installer-home"


def standalone_binary() -> str | None:
    """The standalone build, or None when the official installer has not run."""
    if STANDALONE.is_file() and os.access(STANDALONE, os.X_OK):
        return str(STANDALONE)
    return None


def looks_like_shell_script(text: str) -> bool:
    """A downloaded installer is executed, so it has to prove it is a script first."""
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    return first.startswith("#!") and "sh" in first


DRIFTED = "drifted; restart pending"


@dataclass(slots=True)
class DaemonStatus:
    """What `rc-client codex status` reports, and `rc-client status` summarises."""

    binary: str | None
    path_codex: str | None
    socket: str
    socket_present: bool
    handshake: bool
    supervision: str
    version: control.DaemonVersion | None = None

    @property
    def healthy(self) -> bool:
        return self.handshake

    @property
    def drifted(self) -> bool:
        """Whether the daemon is serving an older app-server than the build on disk.

        Terminals join a drifted daemon regardless, so this is a note rather
        than a fault; the device restarts it when nothing is in it.
        """
        return self.version is not None and self.version.drifted

    def version_lines(self) -> list[str]:
        found = self.version
        if found is None:
            return []
        installed = found.managed or "unknown"
        return [
            f"app-server version  {found.app_server or 'unknown'}",
            f"installed version   {installed}{f' ({DRIFTED})' if self.drifted else ''}",
        ]

    def lines(self) -> list[str]:
        return [
            f"daemon binary       {self.binary or 'standalone not installed'}",
            f"codex on PATH       {self.path_codex or 'not found'}",
            f"daemon socket       {self.socket}",
            f"socket present      {'yes' if self.socket_present else 'no'}",
            f"handshake           {'ok' if self.handshake else 'failed'}",
            *self.version_lines(),
            f"supervision         {self.supervision}",
        ]

    def summary(self) -> str:
        if self.handshake:
            state = f"healthy ({self.supervision})"
        elif self.socket_present:
            state = "socket present but not answering"
        else:
            state = "not running"
        return f"{state}; {DRIFTED}" if self.drifted else state


async def status() -> DaemonStatus:
    present = socket_exists()
    binary = standalone_binary()
    return DaemonStatus(
        binary=binary,
        path_codex=shutil.which("codex"),
        socket=str(socket_path()),
        socket_present=present,
        handshake=await handshake_ok(__version__) if present else False,
        supervision=supervision.status(),
        version=await control.version(binary) if binary and present else None,
    )


def installer_env() -> dict[str, str]:
    """The environment the official installer runs under.

    It takes its prompts off, pins both directories it installs into, and sends
    the shell-profile rewrite it insists on into a home of ours.
    """
    home = installer_home()
    home.mkdir(parents=True, exist_ok=True)
    return dict(
        os.environ,
        CODEX_NON_INTERACTIVE="1",
        HOME=str(home),
        CODEX_HOME=str(CODEX_HOME),
        CODEX_INSTALL_DIR=str(LOCAL_BIN),
    )


async def install_codex() -> tuple[bool, str]:
    """Run the official installer, having read what was downloaded first."""
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(INSTALL_URL)
            response.raise_for_status()
            body = response.text[:MAX_INSTALLER_BYTES]
    except httpx.HTTPError as exc:
        return False, f"could not download the Codex installer: {exc}"
    if not looks_like_shell_script(body):
        return False, f"{INSTALL_URL} did not return a shell script; install Codex by hand"
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "codex-install.sh"
        script.write_text(body, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        process = await asyncio.create_subprocess_exec(
            "/bin/sh",
            str(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=installer_env(),
        )
        out, _ = await process.communicate()
    text = out.decode("utf-8", "replace").strip()
    return process.returncode == 0, text[-2000:]


async def setup(install_missing: bool = True) -> tuple[bool, list[str]]:
    """Bring the shared daemon up on this machine. Returns (ok, printable lines).

    Every step works on the standalone build alone. Whatever `codex` PATH happens
    to resolve to is only ever reported, never used and never removed.
    """
    lines: list[str] = []
    binary = standalone_binary()
    if binary is None:
        if not install_missing:
            lines.append(f"{MISSING}; re-run without --no-install to install it.")
            return False, lines
        ok, detail = await install_codex()
        lines.append(f"Codex installer: {'ok' if ok else 'failed'}")
        if not ok:
            lines.append(f"  {detail}")
            return False, lines
        binary = standalone_binary()
        if not on_path(LOCAL_BIN):
            lines.append(PATH_HINT)
    if binary is None:
        lines.append(f"{MISSING} at {STANDALONE}; the installer did not leave a binary there.")
        return False, lines
    ok, detail = await control.bootstrap(binary)
    lines.append(f"Daemon bootstrap: {detail}")
    if not ok:
        return False, lines
    try:
        target = supervision.install(binary)
        lines.append(f"Supervision installed at {target}")
    except Exception as exc:
        lines.append(f"Supervision could not be installed: {exc}")
    hint = supervision.post_install_hint()
    if hint:
        lines.append(hint)
    current = await status()
    lines.extend(current.lines())
    if not current.healthy:
        lines.append("The daemon did not answer a handshake; see the lines above.")
    return current.healthy, lines
