"""How Claude Code is signed in on this device, and what is left of it (A33).

Everything here is read-only. The OAuth credential comes from the login Keychain
through the `security` CLI — the same tool Claude Code writes it with, so
reading it back asks the Keychain for nothing new — or from
`~/.claude/.credentials.json` on a machine with no Keychain. The account's email
comes from `~/.claude.json`, and a configured key is recognised by name alone.
The device never writes a credential, never refreshes a token, and never logs
one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ...models import UNSET, AgentAccount, PlanSetting
from .. import anthropic_usage, credentials
from ..anthropic_usage import Bearer
from . import runtime

PROVIDER = "anthropic"
VENDOR_HOST = "api.anthropic.com"
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT = 5.0
EXPIRED = "signed-in token expired; open Claude Code once to refresh it"
# Either of these, or an `apiKeyHelper`, puts Claude Code on a key rather than
# on the person's own Anthropic account.
KEY_SETTINGS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
BASE_URL = "ANTHROPIC_BASE_URL"
# A rate-limit tier id reads `default_claude_max_5x`: the vendor's own prefix,
# and then the words that are worth showing a person.
TIER_PREFIX = "default_claude_"

# Returns the Keychain item as the `security` CLI prints it, or nothing.
KeychainReader = Callable[[], Awaitable[str | None]]


def credentials_file() -> Path:
    """Where the OAuth credential lives without a Keychain to hold it."""
    return runtime.CLAUDE_HOME / ".credentials.json"


def settings_files() -> tuple[Path, ...]:
    """Claude Code's own settings, the local override last so that one wins."""
    return (runtime.CLAUDE_HOME / "settings.json", runtime.CLAUDE_HOME / "settings.local.json")


def config_file() -> Path:
    """`~/.claude.json`, which sits beside the home directory rather than in it."""
    return runtime.CLAUDE_HOME.parent / ".claude.json"


async def detect(limits: bool, keychain: KeychainReader | None = None) -> list[AgentAccount]:
    """The one credential Claude Code runs on here, or nothing at all."""
    key = _key_account()
    if key is not None:
        return [key]
    reader = keychain or read_keychain
    signin = _oauth_account(credentials.parse(await _credential(reader)))
    if signin is None:
        return []
    account, bearer = signin
    if limits:
        await anthropic_usage.fill_limits(account, bearer)
    return [account]


async def read_keychain() -> str | None:
    """The Claude Code credential item, as the `security` CLI prints it."""
    if sys.platform != "darwin":
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-w",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), KEYCHAIN_TIMEOUT)
    except TimeoutError:
        process.kill()
        with contextlib.suppress(ProcessLookupError):
            await process.wait()
        return None
    if process.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip() or None


async def _credential(reader: KeychainReader) -> str | None:
    """The stored OAuth credential: the Keychain item, else the file."""
    item = await reader()
    if item:
        return item
    try:
        return credentials_file().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _oauth_account(data: dict[str, Any]) -> tuple[AgentAccount, Bearer | None] | None:
    block = data.get("claudeAiOauth")
    if not isinstance(block, dict):
        return None
    plan = _plan(block.get("subscriptionType"))
    account = AgentAccount(
        provider=PROVIDER,
        method="account",
        plan=plan,
        tier=_tier(block.get("rateLimitTier"), plan),
        email=_email(),
    )
    token = credentials.text(block.get("accessToken"))
    expires = block.get("expiresAt")
    bearer = (
        Bearer(token, expires if isinstance(expires, int) else None, EXPIRED)
        if token is not None
        else None
    )
    return account, bearer


def _plan(value: Any) -> PlanSetting:
    """The subscription word, or `UNSET` so the key is absent when there is none."""
    return credentials.word(value) or UNSET


def _tier(value: Any, plan: PlanSetting) -> str | None:
    """The rate-limit tier in words the device vouches for (A33).

    A tier id of the shape the vendor writes loses that prefix and becomes a
    phrase — `default_claude_max_5x` is `Max 5x` — and a tier in any other
    shape passes through as the vendor wrote it, because the device can vouch
    for nothing more than what it was given. Either way it is dropped when it
    says no more than the plan already does.
    """
    raw = credentials.text(value)
    if raw is None:
        return None
    phrase = raw
    if raw.startswith(TIER_PREFIX):
        words = raw[len(TIER_PREFIX) :].replace("_", " ")
        phrase = words[:1].upper() + words[1:]
    if not phrase or phrase.lower() == plan:
        return None
    return phrase


def _email() -> str | None:
    """The signed-in address Claude Code records, without a network call."""
    account = credentials.table(config_file()).get("oauthAccount")
    return credentials.text(account.get("emailAddress")) if isinstance(account, dict) else None


def _key_account() -> AgentAccount | None:
    """A key in Claude Code's settings or in this daemon's own environment."""
    settings = _settings()
    env = settings["env"]
    configured = settings.get("apiKeyHelper") or any(
        credentials.text(env.get(name)) or os.environ.get(name, "").strip() for name in KEY_SETTINGS
    )
    if not configured:
        return None
    base = credentials.text(env.get(BASE_URL)) or os.environ.get(BASE_URL)
    return AgentAccount(
        provider=PROVIDER,
        method="api_key",
        endpoint=credentials.endpoint(base, VENDOR_HOST),
    )


def _settings() -> dict[str, Any]:
    """The two settings files merged, `settings.local.json` on top."""
    merged: dict[str, Any] = {"env": {}}
    for path in settings_files():
        data = credentials.table(path)
        env = data.get("env")
        if isinstance(env, dict):
            merged["env"].update(env)
        if data.get("apiKeyHelper"):
            merged["apiKeyHelper"] = data["apiKeyHelper"]
    return merged
