"""`rc-client shim` — install, remove and inspect the `claude` wrapper."""

from __future__ import annotations

from pathlib import Path

from . import paths, shellrc, shim

NEW_SHELL_HINT = (
    "Open a new terminal (or `source` the file above) so `claude` resolves to the shim."
)
CONFIRMATION_HINT = (
    "Claude Code asks once per session to confirm the development channel; "
    "choose the local-development option to let this device attach."
)


def install(shell_rc: bool = True, rc_path: Path | None = None) -> list[str]:
    """Write the shim and optionally put its directory in front of PATH."""
    state = shim.install()
    lines = [
        f"shim installed at {state.path}",
        f"mcp config at {state.mcp_config}",
        f"settings at {state.settings}",
    ]
    lines.append(
        f"real claude at {state.real}"
        if state.real
        else "warning: no claude executable found on PATH yet"
    )
    if shell_rc:
        target = rc_path or shellrc.rc_file()
        changed = shellrc.add(target)
        lines.append(f"{'updated' if changed else 'already configured'} {target}")
        lines.append(NEW_SHELL_HINT)
    lines.append(CONFIRMATION_HINT)
    return lines


def remove(shell_rc: bool = True, rc_path: Path | None = None) -> list[str]:
    removal = shim.remove()
    lines = [
        f"removed {paths.shim_path()}" if removal.shim else f"no shim at {paths.shim_path()}",
        f"removed {paths.settings_path()}"
        if removal.settings
        else f"no settings at {paths.settings_path()}",
    ]
    if shell_rc:
        target = rc_path or shellrc.rc_file()
        lines.append(f"{'cleaned' if shellrc.remove(target) else 'nothing to clean in'} {target}")
    return lines


def status() -> list[str]:
    state = shim.status()
    return [
        f"shim         {state.path} ({'installed' if state.installed else 'missing'})",
        f"first on PATH {'yes' if state.on_path else 'no'}",
        f"real claude  {state.real or 'not found'}",
        f"mcp config   {state.mcp_config}",
        f"settings     {state.settings}",
        f"socket       {paths.socket_path()}",
    ]
