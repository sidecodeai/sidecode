"""How pi is signed in on this device, and what is left of it (A33).

pi is the one agent that holds several credentials at once: `~/.pi/agent/auth.json`
is a table keyed by provider, each entry either an OAuth sign-in or a key, so
there is one account per entry in the order the file lists them. Only Anthropic
exposes windows this device can read, and an Anthropic OAuth entry is read with
the same fetcher Claude Code's is. Read-only: nothing is written, refreshed or
logged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...models import AgentAccount
from .. import anthropic_usage, credentials
from ..anthropic_usage import Bearer
from . import runtime

# pi names the Codex provider after the CLI; the wire names the vendor.
PROVIDERS = {"openai-codex": "openai"}
ANTHROPIC = "anthropic"


def auth_file() -> Path:
    return runtime.home() / "agent" / "auth.json"


async def detect(limits: bool) -> list[AgentAccount]:
    """One account per provider pi is signed in to, in the file's own order."""
    accounts: list[AgentAccount] = []
    for name, entry in credentials.table(auth_file()).items():
        made = _account(str(name), entry)
        if made is None:
            continue
        account, bearer = made
        if limits and bearer is not None:
            await anthropic_usage.fill_limits(account, bearer)
        accounts.append(account)
    return accounts


def _account(name: str, entry: Any) -> tuple[AgentAccount, Bearer | None] | None:
    if not isinstance(entry, dict):
        return None
    provider = PROVIDERS.get(name, name)
    kind = entry.get("type")
    if kind == "oauth":
        return AgentAccount(provider=provider, method="account"), _bearer(provider, entry)
    if kind == "api_key" or credentials.text(entry.get("key")) is not None:
        return AgentAccount(provider=provider, method="api_key"), None
    return None


def _bearer(provider: str, entry: dict[str, Any]) -> Bearer | None:
    """The token an Anthropic sign-in can be asked about; nothing for the rest."""
    token = credentials.text(entry.get("access")) if provider == ANTHROPIC else None
    if token is None:
        return None
    expires = entry.get("expires")
    return Bearer(token, expires if isinstance(expires, int) else None)
