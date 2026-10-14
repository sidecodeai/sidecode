"""The oldest build of each separately installed app this gateway still works with (A31, A45, A46).

The web app is served by this gateway and the device client updates itself from the apps, so the
iPhone, Mac, Android and Windows apps are the components installed on their own and therefore the
only ones that can be older than the gateway they talk to. Each has its own floor —
``IOS_MINIMUM_APP_VERSION``, ``MACOS_MINIMUM_APP_VERSION``, ``ANDROID_MINIMUM_APP_VERSION`` and
``WINDOWS_MINIMUM_APP_VERSION`` — and they move separately: an app reads its own entry of `apps`
and no other, so a change that only older Mac builds cannot follow raises the Mac floor and leaves
the other apps' alone.

**Rule for every release, per app: raise an app's constant in the same change that stops the
gateway working with that app's older builds, and leave it alone for an additive change an older
app can ignore.** An app below its minimum shows a blocking "Update required" screen and does
nothing else, so a minimum raised without cause locks working apps out of a gateway that would
have served them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle: config reads the constants below
    from .config import Config

#: The oldest iOS build this gateway still works with, as `major.minor.patch`.
IOS_MINIMUM_APP_VERSION = "0.1.0"

#: The oldest Mac build this gateway still works with, as `major.minor.patch` (A45). It started at
#: the first Mac release, since no older Mac build exists.
MACOS_MINIMUM_APP_VERSION = "1.11.0"

#: The oldest Android build this gateway still works with, as `major.minor.patch` (A46). It started
#: at the first Android release, since no older Android build exists.
ANDROID_MINIMUM_APP_VERSION = "1.12.0"

#: The oldest Windows build this gateway still works with, as `major.minor.patch` (A46). It started
#: at the first Windows release, since no older Windows build exists.
WINDOWS_MINIMUM_APP_VERSION = "1.12.0"

#: The only version form the apps compare, and the one the protocol schema accepts.
RELEASE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def is_release_version(value: str) -> bool:
    """Whether ``value`` is a `major.minor.patch` version, and nothing else: no trailing newline."""
    return RELEASE_VERSION.fullmatch(value) is not None


def apps_view(config: Config) -> dict[str, Any]:
    """The `apps` object carried by `GET /api/health`, `GET /api/config` and `hello`.

    One entry per separately installed app, each read by that app alone (A45, A46).
    """
    return {
        "ios": _app_entry(config.ios_minimum_version, config.ios_update_url),
        "macos": _app_entry(config.macos_minimum_version, config.macos_update_url),
        "android": _app_entry(config.android_minimum_version, config.android_update_url),
        "windows": _app_entry(config.windows_minimum_version, config.windows_update_url),
    }


def _app_entry(minimum_version: str, update_url: str) -> dict[str, str]:
    """One app's entry of `apps`.

    ``update_url`` is present only when the operator configured one: an app with nowhere to send
    the user draws its "Update required" screen without a button rather than a dead link.
    """
    entry = {"minimum_version": minimum_version}
    if update_url:
        entry["update_url"] = update_url
    return entry
