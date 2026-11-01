"""How Claude Code is signed in on this device, and what is left of it (A33).

Everything here is read-only. The OAuth credential comes from the login Keychain
through the `security` CLI (`keychain.py`) or from `~/.claude/.credentials.json`
on a machine with no Keychain. The account's email comes from `~/.claude.json`,
and a configured key is recognised by name alone. The device never writes a
credential, never refreshes a token, and never logs one.

An item the Keychain cannot hand over right now is not a person who signed out:
the account last read is reported again, without its token, until a read says
otherwise.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from ...logging_setup import logger
from ...models import UNSET, AgentAccount, PlanSetting
from .. import anthropic_usage, credentials
from ..anthropic_usage import Bearer
from . import runtime
from .keychain import KeychainUnreadable, read_keychain

log = logger("rc_client.claude.account")

# This agent's id, as `plugin.py` names it.
AGENT = "claude"
PROVIDER = "anthropic"
VENDOR_HOST = "api.anthropic.com"
EXPIRED = "signed-in token expired; open Claude Code once to refresh it"
REJECTED = "signed-in token rejected (HTTP 401); open Claude Code once to refresh it"
KEYCHAIN_BUSY = "the login Keychain could not be read just now"
# Either of these, or an `apiKeyHelper`, puts Claude Code on a key rather than
# on the person's own Anthropic account.
KEY_SETTINGS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
BASE_URL = "ANTHROPIC_BASE_URL"
# A rate-limit tier id reads `default_claude_max_5x`: the vendor's own prefix,
# and then the words that are worth showing a person.
TIER_PREFIX = "default_claude_"

# Returns the Keychain item as the `security` CLI prints it, or nothing; raises
# `KeychainUnreadable` when the item may be there and cannot be read right now.
KeychainReader = Callable[[], Awaitable[str | None]]


@dataclass(slots=True)
class SignIn:
    """The account the credential last held, in memory and without its token."""

    account: AgentAccount | None = None


# What the Keychain last said, for a read that fails now.
signed_in = SignIn()


def credentials_file() -> Path:
    """Where the OAuth credential lives without a Keychain to hold it."""
    return runtime.CLAUDE_HOME / ".credentials.json"


def settings_files() -> tuple[Path, ...]:
    """Claude Code's own settings, the local override last so that one wins."""
    return (runtime.CLAUDE_HOME / "settings.json", runtime.CLAUDE_HOME / "settings.local.json")


def config_file() -> Path:
    """`~/.claude.json`, which sits beside the home directory rather than in it."""
    return runtime.CLAUDE_HOME.parent / ".claude.json"


async def detect(
    limits: bool, keychain: KeychainReader | None = None, proxy: str = ""
) -> list[AgentAccount]:
    """The one credential Claude Code runs on here, or nothing at all."""
    key = _key_account()
    if key is not None:
        return [key]
    reader = keychain or read_keychain
    try:
        raw = await _credential(reader)
    except KeychainUnreadable:
        return _remembered(limits)
    signin = _oauth_account(credentials.parse(raw))
    if signin is None:
        signed_in.account = None
        if raw:
            log.info("claude credential read", outcome="no_oauth_block")
        return []
    account, bearer = signin
    signed_in.account = account.without_limits()
    if limits:
        await anthropic_usage.fill_limits(AGENT, account, bearer, proxy, partial(_renewed, reader))
    return [account]


async def _credential(reader: KeychainReader) -> str | None:
    """The stored OAuth credential: the Keychain item, else the file."""
    item = await reader()
    if item:
        return item
    try:
        return credentials_file().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


async def _renewed(reader: KeychainReader) -> Bearer | None:
    """The token the credential holds now, for a read the vendor refused (it may be new)."""
    try:
        signin = _oauth_account(credentials.parse(await _credential(reader)))
    except KeychainUnreadable:
        return None
    return signin[1] if signin is not None else None


def _remembered(limits: bool) -> list[AgentAccount]:
    """The account last read, when the Keychain cannot be read now; nobody when none was."""
    last = signed_in.account
    if last is None:
        return []
    account = last.without_limits()
    if limits:
        anthropic_usage.fall_back(AGENT, account, KEYCHAIN_BUSY)
    return [account]


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
        Bearer(token, expires if isinstance(expires, int) else None, EXPIRED, REJECTED)
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
