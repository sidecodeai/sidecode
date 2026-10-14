"""How Grok Build is signed in on this device (A33).

`~/.grok/auth.json` holds one entry per issuer the person signed in with, keyed
`<issuer>::<client id>`: an OIDC entry is the vendor's own account and records
the address it belongs to, anything else with a key is a key. Read-only, and
nothing from the entry beyond the mode and the address is ever used.

There are no windows to report. The leader protocol this build speaks is
`session/*` and nothing else, so the device has no way to ask what is left of
the quota — which the wire says by leaving `limits` absent with no error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...models import AgentAccount
from .. import credentials
from . import runtime

PROVIDER = "xai"
OIDC = "oidc"


def auth_file() -> Path:
    return runtime.home() / "auth.json"


async def detect() -> list[AgentAccount]:
    """The credential Grok Build runs on here, or nothing at all."""
    for entry in credentials.table(auth_file()).values():
        account = _account(entry)
        if account is not None:
            return [account]
    return []


def _account(entry: Any) -> AgentAccount | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("auth_mode") == OIDC:
        return AgentAccount(
            provider=PROVIDER,
            method="account",
            email=credentials.text(entry.get("email")),
        )
    if credentials.text(entry.get("key")) is not None:
        return AgentAccount(provider=PROVIDER, method="api_key")
    return None
