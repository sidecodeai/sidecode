"""`rc-client pi setup`, `pi status` and `pi remove`.

`setup` has a contract the installer script depends on: exactly one line on
stdout in every case it can handle, exit 0; a failure is `error: …` on stderr
and exit 1, which the installer reports as a warning rather than giving up on
the whole install.
"""

from __future__ import annotations

from . import install, paths, runtime

NOT_INSTALLED = "pi is not installed on this device; run this again after installing it"


def setup() -> list[str]:
    """Install the extension, or say why there is nothing to install it for."""
    if runtime.resolve_binary() is None:
        return [NOT_INSTALLED]
    before = install.state()
    if before.current:
        return [f"pi extension is current at {before.target}"]
    after = install.install()
    return [f"pi extension installed at {after.target}"]


def status() -> list[str]:
    state = install.state()
    binary = runtime.resolve_binary()
    return [
        f"pi binary      {binary or 'not installed'}",
        f"extension      {state.summary()}",
        f"bundled        {paths.bundled_extension()}",
        f"socket         {paths.socket_path()}",
    ]


def summary() -> str:
    """The one line `rc-client status` prints for the extension."""
    return install.state().summary()


def remove() -> list[str]:
    target = paths.installed_extension()
    if install.remove():
        return [f"removed {target}"]
    return [f"no pi extension at {target}"]
