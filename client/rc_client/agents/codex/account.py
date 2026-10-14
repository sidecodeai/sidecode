"""How the Codex CLI is signed in on this device, and what is left of it (A33).

`~/.codex/auth.json` says which: a ChatGPT account, whose plan and email are
claims of the id token beside it, or an API key, possibly pointed at a
third-party host by `~/.codex/config.toml`. The id token is decoded for those
two claims alone — never verified, never refreshed, never logged. The windows
come from the shared app-server daemon the device is already talking to, which
is where the `/usage` command reads them too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tomllib
from pathlib import Path
from typing import Any

from ...errors import RcError
from ...models import UNSET, AgentAccount, AgentLimit, PlanSetting, now_ms
from .. import credentials
from ..registry import RateLimitsReader
from . import runtime

PROVIDER = "openai"
VENDOR_HOST = "api.openai.com"
BASE_URL = "OPENAI_BASE_URL"
# The id token claim that carries the plan, namespaced by the vendor.
AUTH_CLAIM = "https://api.openai.com/auth"
PLAN_CLAIM = "chatgpt_plan_type"
LIMITS_TIMEOUT = 5.0
# The app-server method that reports what is left of the account's quota (A33).
RATE_LIMITS = "account/rateLimits/read"
NO_DAEMON = "Codex shared daemon is not running"
SLOW_DAEMON = "the Codex shared daemon did not answer in time"


def auth_file() -> Path:
    return runtime.CODEX_HOME / "auth.json"


def config_file() -> Path:
    return runtime.CODEX_HOME / "config.toml"


async def detect(limits: bool, rate_limits: RateLimitsReader | None = None) -> list[AgentAccount]:
    """The one credential Codex runs on here, or nothing at all."""
    auth = credentials.table(auth_file())
    account = _key_account(auth) or _chatgpt_account(auth)
    if account is None:
        return []
    if limits and account.method == "account":
        await _fill_limits(account, rate_limits)
    return [account]


def parse_limits(reply: dict[str, Any]) -> list[AgentLimit]:
    """The windows `account/rateLimits/read` reports, in the order it names them."""
    snapshot = reply.get("rateLimits")
    rows = snapshot if isinstance(snapshot, dict) else {}
    found = (_window(rows.get(key)) for key in ("primary", "secondary"))
    return [limit for limit in found if limit is not None]


def _key_account(auth: dict[str, Any]) -> AgentAccount | None:
    mode = auth.get("auth_mode")
    if mode == "chatgpt":
        return None
    if mode != "apikey" and credentials.text(auth.get("OPENAI_API_KEY")) is None:
        return None
    return AgentAccount(
        provider=PROVIDER,
        method="api_key",
        endpoint=credentials.endpoint(_base_url(), VENDOR_HOST),
    )


def _chatgpt_account(auth: dict[str, Any]) -> AgentAccount | None:
    raw = auth.get("tokens")
    tokens = raw if isinstance(raw, dict) else {}
    if auth.get("auth_mode") != "chatgpt" and not tokens:
        return None
    claims = _claims(tokens.get("id_token"))
    vendor = claims.get(AUTH_CLAIM)
    plan: PlanSetting = (
        credentials.word(vendor.get(PLAN_CLAIM)) or UNSET if isinstance(vendor, dict) else UNSET
    )
    return AgentAccount(
        provider=PROVIDER,
        method="account",
        plan=plan,
        email=credentials.text(claims.get("email")),
    )


def _claims(token: Any) -> dict[str, Any]:
    """The payload of a JWT, decoded and not verified, and never logged."""
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        return {}
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def _base_url() -> str | None:
    """The third-party host a key is sent to, from the config or the environment."""
    try:
        config = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        config = {}
    name = config.get("model_provider")
    providers = config.get("model_providers")
    provider = providers.get(name) if isinstance(providers, dict) and name else None
    if isinstance(provider, dict):
        configured = credentials.text(provider.get("base_url"))
        if configured is not None:
            return configured
    return os.environ.get(BASE_URL)


async def _fill_limits(account: AgentAccount, rate_limits: RateLimitsReader | None) -> None:
    account.limits_checked_at = now_ms()
    if rate_limits is None:
        account.limits_error = NO_DAEMON
        return
    try:
        reply = await asyncio.wait_for(rate_limits(), LIMITS_TIMEOUT)
    except TimeoutError:
        account.limits_error = SLOW_DAEMON
    except RcError as exc:
        account.limits_error = exc.message
    except Exception as exc:  # a quota read must never cost the account itself
        account.limits_error = f"could not read the Codex quota: {type(exc).__name__}"
    else:
        account.limits = parse_limits(reply)


def _window(window: Any) -> AgentLimit | None:
    if not isinstance(window, dict):
        return None
    minutes = window.get("windowDurationMins")
    used = window.get("usedPercent")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
        return None
    if isinstance(used, bool) or not isinstance(used, int | float):
        return None
    return AgentLimit(window_minutes=minutes, used_percent=used, resets_at=_resets(window))


def _resets(window: dict[str, Any]) -> int | None:
    """The daemon counts in seconds; the wire counts in milliseconds."""
    at = window.get("resetsAt")
    return at * 1000 if isinstance(at, int) and not isinstance(at, bool) else None
