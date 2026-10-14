"""The `claude` shim that makes an interactive session attachable.

Claude Code only loads a self-hosted channel behind
`--dangerously-load-development-channels`, so the device wraps the executable
instead of asking the user to remember two flags. The wrapper is deliberately
inert for everything that is not a human at a terminal: the device's own remote
sessions drive `claude` over pipes and must reach the real binary unchanged.

It also passes a `--settings` file carrying a `SessionStart` hook. The channel
bridge can only report the session id the CLI started with, and `/resume`,
`/clear` and a compaction all move the terminal to a different one; the hook is
what tells the daemon where it went. A caller who brings their own `--settings`
keeps it, and the session still attaches - it just has no hook.

An attaching invocation is started inside the pseudo-terminal proxy, which is
what lets the device type into the session (amendment A40). The interpreter is
the one this installation runs on, written in at install time; if it has gone -
a venv rebuilt, a home deleted - the wrapper runs the CLI directly, because a
broken installation of ours must never stand between a person and Claude Code.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from ..config import client_home, ensure_dirs
from . import paths
from .mcp_config import write_mcp_config
from .settings import write_settings

MARKER = "rc-client claude shim"
CHANNEL_FLAG = "--dangerously-load-development-channels"
CHANNEL_VALUE = f"server:{paths.CHANNEL_SERVER_NAME}"

# Flags that mean "this is not an interactive session" or "the caller already
# configured channels itself". Any of them and the shim steps aside.
PASS_THROUGH_FLAGS = (
    "-p",
    "--print",
    "--input-format",
    "--output-format",
    "--sdk-url",
    CHANNEL_FLAG,
    "--mcp-config",
)


def should_attach(argv: list[str], stdin_tty: bool, stdout_tty: bool) -> bool:
    """Whether the shim should append the channel flags to this invocation."""
    if not stdin_tty or not stdout_tty:
        return False
    for token in argv:
        if token in PASS_THROUGH_FLAGS:
            return False
        if any(token.startswith(f"{flag}=") for flag in PASS_THROUGH_FLAGS):
            return False
    return True


def is_shim(path: str | Path) -> bool:
    """True when `path` is a wrapper this device wrote."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return MARKER in head


def real_claude() -> str | None:
    """The first `claude` on PATH that is not one of our wrappers."""
    for candidate in paths.path_candidates("claude"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and not is_shim(candidate):
            return candidate
    return None


def render(real: str) -> str:
    """The wrapper script, with the install-time fallback baked in."""
    return SHIM_TEMPLATE.format(
        marker=MARKER,
        shim_dir=_quote(str(paths.bin_dir())),
        real=_quote(real),
        home=_quote(str(client_home())),
        mcp_config=_quote(str(paths.mcp_config_path())),
        settings=_quote(str(paths.settings_path())),
        python=_quote(paths.python_executable()),
        pty_module=paths.pty_module(),
        channel_flag=CHANNEL_FLAG,
        channel_value=CHANNEL_VALUE,
    )


def _quote(value: str) -> str:
    """Single-quote a value for POSIX sh."""
    return "'" + value.replace("'", "'\\''") + "'"


@dataclass(slots=True)
class ShimStatus:
    installed: bool
    path: str
    on_path: bool
    real: str | None
    mcp_config: str
    settings: str

    @property
    def ready(self) -> bool:
        return self.installed and self.on_path


@dataclass(frozen=True, slots=True)
class ShimRemoval:
    """What `remove` actually deleted, so the CLI can say so."""

    shim: bool
    settings: bool


def status() -> ShimStatus:
    target = paths.shim_path()
    installed = target.is_file() and is_shim(target)
    resolved = shutil.which("claude")
    return ShimStatus(
        installed=installed,
        path=str(target),
        on_path=bool(resolved and is_shim(resolved)),
        real=real_claude(),
        mcp_config=str(paths.mcp_config_path()),
        settings=str(paths.settings_path()),
    )


def install() -> ShimStatus:
    """Write the wrapper, the MCP config and the settings file; all three every call."""
    ensure_dirs()
    target = paths.shim_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    real = real_claude() or ""
    target.write_text(render(real), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)
    write_mcp_config()
    write_settings()
    return status()


def remove() -> ShimRemoval:
    """Delete the wrapper and its settings file, leaving anything we did not write alone."""
    settings_file = paths.settings_path()
    removed_settings = settings_file.is_file()
    settings_file.unlink(missing_ok=True)
    target = paths.shim_path()
    if not target.is_file() or not is_shim(target):
        return ShimRemoval(shim=False, settings=removed_settings)
    target.unlink()
    return ShimRemoval(shim=True, settings=removed_settings)


SHIM_TEMPLATE = """#!/bin/sh
# {marker}
#
# Appends the Claude Code channel flags when a person starts an interactive
# session, so the remote-control device can attach to it, plus a settings file
# whose SessionStart hook says which session the terminal moved to, and runs it
# inside the device's pseudo-terminal proxy so the session can be typed into.
# Every other invocation - piped, --print, or already carrying channel flags -
# reaches the real executable untouched. Managed by `rc-client shim install`.
set -u

RC_SHIM_DIR={shim_dir}
RC_FALLBACK_CLAUDE={real}
RC_CLIENT_HOME=${{RC_CLIENT_HOME:-{home}}}
RC_MCP_CONFIG={mcp_config}
RC_SETTINGS={settings}
RC_PYTHON={python}
export RC_CLIENT_HOME

is_shim() {{
    head -n 3 "$1" 2>/dev/null | grep -q '{marker}'
}}

find_real() {{
    saved_ifs=$IFS
    IFS=:
    for dir in $PATH; do
        [ -n "$dir" ] || dir=.
        if [ "$dir" != "$RC_SHIM_DIR" ] && [ -x "$dir/claude" ] && ! is_shim "$dir/claude"; then
            IFS=$saved_ifs
            printf '%s\\n' "$dir/claude"
            return 0
        fi
    done
    IFS=$saved_ifs
    return 1
}}

REAL=$(find_real) || REAL=$RC_FALLBACK_CLAUDE
if [ -z "$REAL" ] || [ ! -x "$REAL" ]; then
    printf 'rc-client: cannot find the real claude executable\\n' >&2
    exit 127
fi

attach=1
[ -t 0 ] && [ -t 1 ] || attach=0
[ -f "$RC_MCP_CONFIG" ] || attach=0

# Settings of the caller's own win: the hook is then simply absent, and the
# session still attaches.
add_settings=1
[ -f "$RC_SETTINGS" ] || add_settings=0

for arg in "$@"; do
    case "$arg" in
        -p|--print|--input-format|--output-format|--sdk-url|{channel_flag}|--mcp-config) attach=0 ;;
        --print=*|--input-format=*|--output-format=*|--sdk-url=*|--mcp-config=*) attach=0 ;;
        {channel_flag}=*) attach=0 ;;
        --settings|--settings=*) add_settings=0 ;;
    esac
done

if [ "$attach" -eq 1 ]; then
    set -- "$@" {channel_flag} {channel_value} --mcp-config "$RC_MCP_CONFIG"
    if [ "$add_settings" -eq 1 ]; then
        set -- "$@" --settings "$RC_SETTINGS"
    fi
    # The pseudo-terminal the device types into. An interpreter that is gone
    # means our installation is broken, and that must not cost you Claude Code.
    if [ -x "$RC_PYTHON" ]; then
        exec "$RC_PYTHON" -m {pty_module} -- "$REAL" "$@"
    fi
fi
exec "$REAL" "$@"
"""
