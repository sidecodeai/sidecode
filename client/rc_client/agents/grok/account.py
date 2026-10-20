"""How Grok Build is signed in on this device, and what is left of it (A33).

`~/.grok/auth.json` holds one entry per issuer the person signed in with, keyed
`<issuer>::<client id>`: an OIDC entry is the vendor's own account and records
the address it belongs to, anything else with a key is a key. Read-only, and
nothing from the entry beyond the mode and the address is ever used.

The account's window comes from the leader the device joins, which reads it the
way `/usage` does (A53). With leader mode off there is none to report, which
the wire says by leaving `limits` absent with no error; while the device is
still joining the leader, `limits_error` says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...models import AgentAccount
from .. import credentials
from ..registry import BillingReader
from . import quota, runtime

PROVIDER = "xai"
OIDC = "oidc"


def auth_file() -> Path:
    return runtime.home() / "auth.json"


async def detect(limits: bool = False, billing: BillingReader | None = None) -> list[AgentAccount]:
    """The credential Grok Build runs on here, or nothing at all."""
    for entry in credentials.table(auth_file()).values():
        account = _account(entry)
        if account is None:
            continue
        if limits and billing is not None and account.method == "account":
            await quota.fill_limits(account, billing)
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
