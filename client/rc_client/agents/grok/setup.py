"""`rc-client grok setup` and `rc-client grok status`.

`setup` turns `[cli] use_leader` on in the person's own `~/.grok/config.toml`,
which is what lets the apps join a `grok` started in a terminal (A28). It is
their file, so it is edited **in place**: the existing path is opened for
writing, never unlinked, renamed or replaced, because these dotfiles are often
symlinks into a synced folder and the inode has to survive. Every byte but the
one line is left exactly as it was, comments included.

Nothing here starts or stops anything a person is using. A `grok` already
running keeps the agent it started with, so both commands end by saying so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import leader, runtime
from . import sessions as grok_sessions

SECTION = "[cli]"
OPTION = "use_leader"
RESTART_NOTE = "Restart any running grok to have it join the leader"
SANDBOX_NOTE = "A sandbox profile refuses leader mode; set [sandbox] profile to off to attach"

_HEADER = re.compile(r"^\s*\[cli\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_OPTION = re.compile(rf"^(\s*){OPTION}\s*=")


def enable(text: str) -> str:
    """`text` with `[cli] use_leader = true` set, and nothing else touched."""
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if _HEADER.match(line)), None)
    if start is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        gap = "\n" if text.strip() else ""
        return f"{text}{prefix}{gap}{SECTION}\n{OPTION} = true\n"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _ANY_HEADER.match(lines[index]):
            end = index
            break
    for index in range(start + 1, end):
        found = _OPTION.match(lines[index])
        if found is None:
            continue
        ending = "\n" if lines[index].endswith("\n") else ""
        lines[index] = f"{found.group(1)}{OPTION} = true{ending}"
        return "".join(lines)
    lines.insert(start + 1, f"{OPTION} = true\n")
    return "".join(lines)


def write_config(path: Path, text: str) -> None:
    """Write the file where it already is, keeping its inode and its symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)


def turn_on() -> bool:
    """Set the flag. True when the file changed, False when it was already on."""
    path = runtime.config_file()
    try:
        before = path.read_text(encoding="utf-8")
    except OSError:
        before = ""
    after = enable(before)
    if after == before and before:
        return False
    write_config(path, after)
    return True


@dataclass(slots=True)
class LeaderStatus:
    """What `rc-client grok status` reports, and `rc-client status` summarises."""

    binary: str | None
    use_leader: bool
    sandbox: str
    socket: str
    socket_present: bool
    registered: int
    leader_version: str | None = None
    installed_version: str | None = None

    @property
    def ready(self) -> bool:
        """Whether the next `grok` started here will join the leader (4.2)."""
        return self.use_leader and self.sandbox == leader.NO_SANDBOX

    @property
    def drifted(self) -> bool:
        """Whether a running leader is older than the Grok installed on disk."""
        return (
            self.leader_version is not None
            and self.installed_version is not None
            and self.leader_version != self.installed_version
        )

    def lines(self) -> list[str]:
        found = [
            f"grok binary         {self.binary or 'not installed'}",
            f"use_leader          {'on' if self.use_leader else 'off'}",
            f"sandbox             {self.sandbox}",
            f"leader socket       {self.socket}{'' if self.socket_present else ' (absent)'}",
        ]
        if self.leader_version is not None:
            found.append(f"leader version      {self.leader_version}")
            found.append(
                f"installed version   {self.installed_version or 'unknown'}"
                f"{' (leader is older; restart it)' if self.drifted else ''}"
            )
        found.append(f"terminal sessions   {self.registered} registered")
        if not self.ready and self.sandbox != leader.NO_SANDBOX:
            found.append(SANDBOX_NOTE)
        found.append(RESTART_NOTE)
        return found

    def summary(self) -> str:
        """The one line `rc-client status` prints for the leader."""
        if self.binary is None:
            return "grok is not installed"
        if not self.use_leader:
            return "use_leader is off; run 'rc-client grok setup'"
        if self.sandbox != leader.NO_SANDBOX:
            return f"sandbox profile {self.sandbox} refuses leader mode"
        state = "running" if self.leader_version else "not started yet"
        return f"ready ({state}); {self.registered} terminal session(s)"


async def status(*, probe: bool = True) -> LeaderStatus:
    """Read the configuration, and ask a leader its version when one may exist.

    `probe` connects, which starts a leader if none is running. That is exactly
    what the next `grok` would do, so it is never a surprise; it is off for the
    one-line summary that `rc-client status` prints.
    """
    config = leader.read_config()
    binary = runtime.resolve_binary()
    entries, _ = grok_sessions.active_entries()
    found = LeaderStatus(
        binary=binary,
        use_leader=leader.use_leader(config),
        sandbox=leader.sandbox_profile(config),
        socket=str(leader.socket_path()),
        socket_present=leader.socket_path().exists(),
        registered=sum(1 for entry in entries if entry.live),
    )
    if not probe or binary is None or not found.ready:
        return found
    client = leader.LeaderClient(binary)
    if await client.connect():
        found.leader_version = client.version
        found.installed_version = await runtime.probe_version(binary)
    await client.close()
    return found


async def setup() -> tuple[bool, list[str]]:
    """Turn the flag on, then report. Returns (ready, printable lines)."""
    path = runtime.config_file()
    changed = turn_on()
    lines = [
        f"{'Set' if changed else 'Already set'} {SECTION} {OPTION} = true in {path}",
    ]
    current = await status()
    lines.extend(current.lines())
    return current.ready, lines
