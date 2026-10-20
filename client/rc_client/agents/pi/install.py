"""Put the extension where pi discovers it, and tell whether it is current.

pi loads every `*.ts` in `~/.pi/agent/extensions/` without being configured to,
so installing is a file copy and nothing else: the person's own
`settings.json` is never opened, let alone written.

"Current" means byte-equal to the copy inside the wheel. That is what
`AgentInfo.attach_ready` reports, and it is why an `rc-client` update that
changes the extension makes every pi session pick the new one up as soon as it
is installed again.

Earlier builds installed the file as `remote-control.ts`. A copy under that name
that carries the extension's marker is ours: it counts as installed and stale,
so an update refreshes it, installing moves it onto the new name before
replacing it, and removing takes it out. A file under that name without the
marker is somebody else's and is left alone.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ...errors import RcError
from . import paths


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


@dataclass(slots=True, frozen=True)
class ExtensionState:
    """Where the extension is and whether pi would load this build of it."""

    target: Path
    installed: bool
    current: bool

    def summary(self) -> str:
        if not self.installed:
            return f"not installed ({self.target})"
        return f"{'current' if self.current else 'stale'} at {self.target}"


def _legacy() -> Path | None:
    """An earlier build's copy under the old name, when it is ours."""
    path = paths.legacy_extension()
    body = _read(path)
    return path if body is not None and paths.MARKER in body else None


def state() -> ExtensionState:
    target = paths.installed_extension()
    installed = _read(target)
    legacy = _legacy()
    if installed is None:
        if legacy is not None:
            return ExtensionState(target=legacy, installed=True, current=False)
        return ExtensionState(target=target, installed=False, current=False)
    # pi loads a copy left under the old name as well, and that one may act.
    current = legacy is None and installed == _read(paths.bundled_extension())
    return ExtensionState(target=target, installed=True, current=current)


def ready() -> bool:
    """`attach_ready`: pi will load this device's extension, at this build."""
    return state().current


def install() -> ExtensionState:
    """Copy the bundled extension into pi's global extension directory."""
    source = paths.bundled_extension()
    target = paths.installed_extension()
    body = _read(source)
    if body is None:
        raise RcError("bad_request", f"the bundled pi extension is missing at {source}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(body)
        legacy = _legacy()
        if legacy is not None:
            # Renamed rather than deleted, so pi finds exactly one copy throughout.
            legacy.replace(target)
        # A pi starting while this runs must see one file or the other, never
        # half of one, because jiti compiles whatever it finds.
        shutil.move(str(temporary), str(target))
    except OSError as exc:
        raise RcError("bad_request", f"could not install the pi extension: {exc}") from exc
    return state()


def remove() -> list[Path]:
    """Take the extension out again, under either name. The files it removed."""
    removed: list[Path] = []
    for target in (paths.installed_extension(), _legacy()):
        if target is None:
            continue
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RcError("bad_request", f"could not remove the pi extension: {exc}") from exc
        removed.append(target)
    return removed
