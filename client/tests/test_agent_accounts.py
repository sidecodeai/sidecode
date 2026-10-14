"""How each agent is signed in here, and what is left of its quota (A33).

Every credential in this file is synthetic and written into `tmp_path`: the
suite never reads a person's own `~/.claude`, `~/.codex`, `~/.grok` or `~/.pi`,
and never asks the login Keychain for anything — `conftest.py` moves the homes
and replaces the Keychain reader for the whole run.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from rc_client import daemon as daemon_module
from rc_client.agents import anthropic_usage
from rc_client.agents.claude import account as claude_account
from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.codex import account as codex_account
from rc_client.agents.codex import runtime as codex_runtime
from rc_client.agents.grok import account as grok_account
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.pi import account as pi_account
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.agents.registry import DetectContext
from rc_client.config import Config, ensure_dirs
from rc_client.daemon import Daemon
from rc_client.models import AgentAccount, AgentInfo, AgentLimit, now_ms

# The usage body `GET /api/oauth/usage` answers with, emptied of anything
# private: a five-hour window, a week, and a week confined to one model.
USAGE_BODY: dict[str, Any] = {
    "five_hour": {"utilization": 16.0, "resets_at": "2026-09-15T15:40:00.000000+00:00"},
    "seven_day": {"utilization": 54.0, "resets_at": "2026-09-16T22:00:00.000000+00:00"},
    "seven_day_opus": None,
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 16,
            "severity": "normal",
            "resets_at": "2026-09-15T15:40:00.000000+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 54,
            "severity": "normal",
            "resets_at": "2026-09-16T22:00:00.000000+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 64,
            "severity": "normal",
            "resets_at": "2026-09-16T22:00:00.000000+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True,
        },
    ],
}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def claude_credential(expires_at: int, subscription: str = "max") -> dict[str, Any]:
    return {
        "claudeAiOauth": {
            "accessToken": "test-access-token",
            "refreshToken": "test-refresh-token",
            "expiresAt": expires_at,
            "subscriptionType": subscription,
            "rateLimitTier": "default_claude_max_5x",
        }
    }


def id_token(claims: dict[str, Any]) -> str:
    """A JWT whose payload is `claims`; only the payload is ever decoded."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii")
    return f"header.{payload.rstrip('=')}.signature"


def usage_calls(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any] | None) -> list[str]:
    """Stand in for the network, and record that it was reached at all."""
    calls: list[str] = []

    async def fetch(token: str) -> tuple[dict[str, Any] | None, str]:
        calls.append("fetched")
        return (body, "") if body is not None else (None, "could not reach api.anthropic.com")

    monkeypatch.setattr(anthropic_usage, "fetch", fetch)
    return calls


# ------------------------------------------------------------------- Claude


async def test_a_claude_oauth_credential_is_an_account_with_its_tier_and_email() -> None:
    write_json(claude_account.credentials_file(), claude_credential(now_ms() + 3_600_000))
    write_json(claude_account.config_file(), {"oauthAccount": {"emailAddress": "me@example.com"}})

    accounts = await claude_account.detect(limits=False)

    assert [account.to_dict() for account in accounts] == [
        {
            "provider": "anthropic",
            "method": "account",
            "plan": "max",
            "tier": "Max 5x",
            "email": "me@example.com",
        }
    ]


@pytest.mark.parametrize(
    ("raw", "plan", "expected"),
    [
        ("default_claude_max_5x", "max", "Max 5x"),
        ("default_claude_max_20x", "max", "Max 20x"),
        ("default_claude_pro", "max", "Pro"),
        # A tier that says no more than the plan is left out altogether (A33).
        ("default_claude_pro", "pro", None),
        # A shape the vendor's prefix does not cover passes through unchanged.
        ("enterprise_tier", "enterprise", "enterprise_tier"),
    ],
)
async def test_the_rate_limit_tier_travels_in_words_the_device_vouches_for(
    raw: str, plan: str, expected: str | None
) -> None:
    credential = claude_credential(now_ms() + 3_600_000, subscription=plan)
    credential["claudeAiOauth"]["rateLimitTier"] = raw
    write_json(claude_account.credentials_file(), credential)

    accounts = await claude_account.detect(limits=False)

    assert accounts[0].tier == expected


async def test_a_claude_credential_in_the_keychain_is_read_through_the_injected_command() -> None:
    async def keychain() -> str:
        return json.dumps(claude_credential(now_ms() + 3_600_000, subscription="pro"))

    accounts = await claude_account.detect(limits=False, keychain=keychain)

    assert [(account.method, account.plan) for account in accounts] == [("account", "pro")]


async def test_a_configured_key_is_reported_with_the_host_it_is_sent_to() -> None:
    write_json(claude_account.credentials_file(), claude_credential(now_ms() + 3_600_000))
    write_json(
        claude_runtime.CLAUDE_HOME / "settings.json",
        {"env": {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_BASE_URL": "https://relay.example/v1"}},
    )

    accounts = await claude_account.detect(limits=False)

    assert [account.to_dict() for account in accounts] == [
        {"provider": "anthropic", "method": "api_key", "endpoint": "relay.example"}
    ]


async def test_a_key_pointed_at_anthropic_itself_names_no_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

    accounts = await claude_account.detect(limits=False)

    assert [account.to_dict() for account in accounts] == [
        {"provider": "anthropic", "method": "api_key"}
    ]


async def test_claude_is_signed_in_nowhere_when_there_is_no_credential_to_read() -> None:
    assert await claude_account.detect(limits=False) == []


async def test_a_malformed_credential_file_is_signed_in_nowhere_rather_than_an_error() -> None:
    path = claude_account.credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert await claude_account.detect(limits=False) == []


async def test_the_usage_body_becomes_a_session_a_week_and_a_scoped_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(claude_account.credentials_file(), claude_credential(now_ms() + 3_600_000))
    calls = usage_calls(monkeypatch, USAGE_BODY)

    accounts = await claude_account.detect(limits=True)

    assert calls == ["fetched"]
    account = accounts[0]
    assert account.limits is not None
    assert [limit.to_dict() for limit in account.limits] == [
        {"window_minutes": 300, "used_percent": 16, "resets_at": 1789486800000},
        {"window_minutes": 10080, "used_percent": 54, "resets_at": 1789596000000},
        {"window_minutes": 10080, "scope": "Fable", "used_percent": 64, "resets_at": 1789596000000},
    ]
    assert account.limits_error is None
    assert account.limits_checked_at is not None


def test_a_usage_body_without_rows_falls_back_to_the_older_pair() -> None:
    body = {key: value for key, value in USAGE_BODY.items() if key != "limits"}

    assert [limit.to_dict() for limit in anthropic_usage.parse(body)] == [
        {"window_minutes": 300, "used_percent": 16, "resets_at": 1789486800000},
        {"window_minutes": 10080, "used_percent": 54, "resets_at": 1789596000000},
    ]


async def test_an_expired_token_is_reported_rather_than_refreshed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(claude_account.credentials_file(), claude_credential(now_ms() - 1_000))
    calls = usage_calls(monkeypatch, USAGE_BODY)

    accounts = await claude_account.detect(limits=True)

    assert calls == [], "an expired token must never be sent anywhere"
    assert accounts[0].limits is None
    assert accounts[0].limits_error == claude_account.EXPIRED
    assert accounts[0].limits_checked_at is not None


async def test_a_usage_call_that_fails_leaves_the_account_with_one_line_of_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(claude_account.credentials_file(), claude_credential(now_ms() + 3_600_000))
    usage_calls(monkeypatch, None)

    accounts = await claude_account.detect(limits=True)

    assert accounts[0].limits is None
    assert accounts[0].limits_error == "could not reach api.anthropic.com"


# -------------------------------------------------------------------- Codex


async def test_a_codex_chatgpt_signin_carries_the_plan_and_email_from_its_id_token() -> None:
    write_json(
        codex_account.auth_file(),
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": id_token(
                    {
                        "email": "me@example.com",
                        "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"},
                    }
                ),
                "access_token": "test-access-token",
                "account_id": "acct-1",
            },
        },
    )

    accounts = await codex_account.detect(limits=False)

    assert [account.to_dict() for account in accounts] == [
        {"provider": "openai", "method": "account", "plan": "pro", "email": "me@example.com"}
    ]


async def test_the_codex_daemon_windows_are_seconds_turned_into_milliseconds() -> None:
    write_json(
        codex_account.auth_file(),
        {"auth_mode": "chatgpt", "tokens": {"id_token": id_token({"email": "me@example.com"})}},
    )

    async def rate_limits() -> dict[str, Any]:
        return {
            "rateLimits": {
                "planType": "pro",
                "primary": {"usedPercent": 37, "windowDurationMins": 300, "resetsAt": 1789484400},
                "secondary": {
                    "usedPercent": 12,
                    "windowDurationMins": 10080,
                    "resetsAt": 1789900800,
                },
            }
        }

    accounts = await codex_account.detect(limits=True, rate_limits=rate_limits)

    account = accounts[0]
    assert account.limits is not None
    assert [limit.to_dict() for limit in account.limits] == [
        {"window_minutes": 300, "used_percent": 37, "resets_at": 1789484400000},
        {"window_minutes": 10080, "used_percent": 12, "resets_at": 1789900800000},
    ]
    assert account.limits_error is None


async def test_a_codex_account_with_no_shared_daemon_says_so_in_one_line() -> None:
    write_json(codex_account.auth_file(), {"auth_mode": "chatgpt", "tokens": {}})

    accounts = await codex_account.detect(limits=True)

    assert accounts[0].limits is None
    assert accounts[0].limits_error == codex_account.NO_DAEMON
    assert accounts[0].limits_checked_at is not None


async def test_a_codex_key_is_reported_with_the_third_party_host_its_config_names() -> None:
    write_json(codex_account.auth_file(), {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"})
    (codex_runtime.CODEX_HOME / "config.toml").write_text(
        'model_provider = "relay"\n\n[model_providers.relay]\nbase_url = "https://api.relay.example/v1"\n',
        encoding="utf-8",
    )

    accounts = await codex_account.detect(limits=False)

    assert [account.to_dict() for account in accounts] == [
        {"provider": "openai", "method": "api_key", "endpoint": "api.relay.example"}
    ]


async def test_codex_is_signed_in_nowhere_when_its_auth_file_is_missing() -> None:
    assert await codex_account.detect(limits=False) == []


# --------------------------------------------------------------- Grok Build


async def test_a_grok_oidc_entry_is_an_account_with_no_windows_and_no_error() -> None:
    write_json(
        grok_account.auth_file(),
        {
            "https://accounts.x.ai::grok-cli": {
                "key": "test-key",
                "auth_mode": "oidc",
                "email": "me@example.com",
                "principal_type": "User",
                "refresh_token": "test-refresh-token",
                "expires_at": 1789900800,
            }
        },
    )

    accounts = await grok_account.detect()

    assert [account.to_dict() for account in accounts] == [
        {"provider": "xai", "method": "account", "email": "me@example.com"}
    ]
    assert accounts[0].limits is None and accounts[0].limits_error is None


async def test_a_grok_entry_that_is_only_a_key_is_a_key() -> None:
    write_json(grok_account.auth_file(), {"console": {"key": "xai-test", "auth_mode": "api_key"}})

    assert [account.to_dict() for account in await grok_account.detect()] == [
        {"provider": "xai", "method": "api_key"}
    ]


async def test_grok_is_signed_in_nowhere_without_an_auth_file() -> None:
    assert await grok_account.detect() == []


# ----------------------------------------------------------------------- pi


async def test_pi_reports_one_account_per_provider_and_renames_the_codex_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(
        pi_account.auth_file(),
        {
            "anthropic": {
                "type": "oauth",
                "access": "test-access-token",
                "refresh": "test-refresh-token",
                "expires": now_ms() + 3_600_000,
            },
            "openai-codex": {"type": "api_key", "key": "sk-test"},
            "xai": {"type": "oauth", "access": "test-access-token", "expires": now_ms() + 600_000},
        },
    )
    calls = usage_calls(monkeypatch, USAGE_BODY)

    accounts = await pi_account.detect(limits=True)

    assert [(account.provider, account.method) for account in accounts] == [
        ("anthropic", "account"),
        ("openai", "api_key"),
        ("xai", "account"),
    ]
    # Only Anthropic exposes windows this device can read; the others carry
    # neither limits nor an error, which is the wire's "there are none".
    assert calls == ["fetched"]
    assert accounts[0].limits is not None and len(accounts[0].limits) == 3
    assert accounts[2].limits is None and accounts[2].limits_error is None


async def test_pi_is_signed_in_nowhere_when_its_auth_file_is_missing() -> None:
    assert await pi_account.detect(limits=False) == []


# ------------------------------------------------------- what each frame says


async def test_an_agent_that_is_not_installed_does_not_say_it_looked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rc_client.agents.grok.plugin import detect as detect_grok

    # Grok's launcher lives at a fixed path, so emptying PATH is not enough to
    # make this machine look like one without Grok Build installed.
    monkeypatch.setattr(grok_runtime, "resolve_binary", lambda: None)
    write_json(
        grok_account.auth_file(), {"issuer": {"auth_mode": "oidc", "email": "a@example.com"}}
    )

    info = await detect_grok(DetectContext())

    assert info.available is False
    assert info.accounts is None
    assert "accounts" not in info.to_dict()


async def test_only_the_device_agents_reply_carries_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_dirs()
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
        )
    )

    async def detect_all(context: Any = None) -> list[AgentInfo]:
        windows = [AgentLimit(300, 16, resets_at=1789486800000)] if context.limits else None
        return [
            AgentInfo(
                agent="claude",
                available=True,
                accounts=[
                    AgentAccount(
                        provider="anthropic",
                        method="account",
                        plan="max",
                        limits=windows,
                        limits_checked_at=now_ms() if context.limits else None,
                    )
                ],
            )
        ]

    monkeypatch.setattr(daemon_module, "detect_all", detect_all)
    try:
        reply = await daemon._device_agents({})
        hello = await daemon._hello()
        stored = [info.to_dict() for info in daemon.agents]
        fresh = [info.to_dict() for info in await detect_all(daemon._detect_context())]
    finally:
        daemon.registry.close()

    assert reply["agents"][0]["accounts"][0]["limits"] == [
        {"window_minutes": 300, "used_percent": 16, "resets_at": 1789486800000}
    ]
    assert hello["agents"][0]["accounts"] == [
        {"provider": "anthropic", "method": "account", "plan": "max"}
    ]
    assert stored == hello["agents"]
    # The refresh loop diffs exactly these two lists, so a reply with quota in
    # it must leave nothing behind for the loop to call news (A33).
    assert fresh == stored


async def test_the_daemon_asks_the_shared_codex_daemon_only_when_quota_is_wanted() -> None:
    ensure_dirs()
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
        )
    )
    try:
        assert daemon._detect_context().codex_rate_limits is None
        # No daemon connection in a test, so there is nothing to ask even then.
        assert daemon.codex.ready is False
        assert daemon._detect_context(limits=True).codex_rate_limits is None
    finally:
        daemon.registry.close()


def test_the_homes_these_tests_read_are_never_a_real_one(tmp_path: Path) -> None:
    """The guard `conftest.py` installs, asserted once so it cannot rot."""
    for path in (
        claude_account.credentials_file(),
        claude_account.config_file(),
        codex_account.auth_file(),
        grok_account.auth_file(),
        pi_account.auth_file(),
    ):
        assert tmp_path in path.parents, path
    assert Path.home() / ".claude" != claude_runtime.CLAUDE_HOME
    assert Path.home() / ".codex" != codex_runtime.CODEX_HOME
    assert grok_runtime.home() != Path.home() / ".grok"
    assert pi_runtime.home() != Path.home() / ".pi"
