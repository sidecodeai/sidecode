"""Grok Build's quota, read from the leader the way `/usage` reads it (A53).

The replies are the leader's own, recorded from Grok Build 1.0.62 against a
local stand-in for xAI that answered with made-up periods and amounts
(`fixtures/grok/billing.json`); for `unreachable` the stand-in's address was
one that does not resolve. A reply without `creditUsagePercent` is 0%, which is
how Grok's TUI shows such a reply.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rc_client import daemon as daemon_module
from rc_client.agents.grok import account as grok_account
from rc_client.agents.grok import quota
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok import service as grok_service
from rc_client.agents.grok.acp import GrokError
from rc_client.agents.grok.plugin import detect as detect_grok
from rc_client.agents.grok.service import GrokLeaderService
from rc_client.agents.registry import BillingReader, DetectContext
from rc_client.config import Config, ensure_dirs
from rc_client.daemon import Daemon
from rc_client.models import AgentAccount, AgentInfo, AgentLimit
from tests.test_grok_runner import Peer, wait_for
from tests.test_grok_service import (
    SESSION,
    build,
    connected,
    leader_peer,
    register,
    shared_session,
    write_summary,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"
RECORDED: dict[str, Any] = json.loads((FIXTURES / "billing.json").read_text(encoding="utf-8"))
# When the recorded week and month end, in the wire's milliseconds.
WEEK_ENDS = int(datetime(2026, 10, 12, tzinfo=UTC).timestamp() * 1000)
MONTH_ENDS = int(datetime(2026, 11, 1, tzinfo=UTC).timestamp() * 1000)
EMAIL = "me@example.com"


@pytest.fixture
def leader_mode(monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Grok home whose configuration asks for the leader, signed in to an account."""
    home = grok_runtime.home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[cli]\nuse_leader = true\n", encoding="utf-8")
    monkeypatch.delenv("GROK_SANDBOX", raising=False)
    sign_in()
    return home


def sign_in(entry: dict[str, Any] | None = None) -> None:
    home = grok_runtime.home()
    home.mkdir(parents=True, exist_ok=True)
    signed = entry or {"key": "test-key", "auth_mode": "oidc", "email": EMAIL}
    grok_account.auth_file().write_text(
        json.dumps({"https://auth.x.ai::grok-cli": signed}), encoding="utf-8"
    )


def recorded(case: str) -> dict[str, Any]:
    result: dict[str, Any] = RECORDED[case]["result"]
    return result


def answering(case: str) -> BillingReader:
    """A leader that answers the way the recording says it did."""

    async def billing() -> dict[str, Any]:
        reply = RECORDED[case]
        if "error" in reply:
            error = reply["error"]
            raise GrokError(error["message"], error["code"], error.get("data"))
        return dict(reply["result"])

    return billing


# --------------------------------------------------------------- the window


def test_a_week_is_one_seven_day_window_that_resets_when_it_ends() -> None:
    assert quota.parse_limits(recorded("weekly")) == [AgentLimit(10080, 37.5, WEEK_ENDS)]


def test_a_period_xai_names_no_share_of_is_unused() -> None:
    """proto3 JSON leaves the zero out; the TUI calls the same reply 0%."""
    assert quota.parse_limits(recorded("weekly_unused")) == [AgentLimit(10080, 0, WEEK_ENDS)]


def test_a_month_is_as_long_as_that_month() -> None:
    assert quota.parse_limits(recorded("monthly")) == [AgentLimit(44640, 12, MONTH_ENDS)]


def test_a_reply_with_no_period_has_no_window() -> None:
    assert quota.parse_limits(recorded("no_period")) == []


@pytest.mark.parametrize(
    "change",
    [
        {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_DAILY"}},
        {"currentPeriod": {"type": "USAGE_PERIOD_TYPE_UNSPECIFIED"}},
        {"currentPeriod": {"end": None}},
        {"currentPeriod": {"start": "last monday"}},
        {"currentPeriod": {"end": "2026-10-05T00:00:00+00:00"}},
        {"creditUsagePercent": "37.5"},
        {"creditUsagePercent": True},
    ],
)
def test_a_period_the_device_cannot_read_has_no_window(change: dict[str, Any]) -> None:
    config = json.loads(json.dumps(recorded("weekly")["config"]))
    for key, value in change.items():
        if isinstance(value, dict):
            config[key].update(value)
        else:
            config[key] = value
    assert quota.parse_limits({"config": config}) == []


def test_a_reply_without_a_config_has_no_window() -> None:
    assert quota.parse_limits({}) == []
    assert quota.parse_limits({"config": "weekly"}) == []


def test_a_time_without_an_offset_is_utc() -> None:
    config = json.loads(json.dumps(recorded("weekly")["config"]))
    config["currentPeriod"].update(start="2026-10-05T09:30:00", end="2026-10-12T09:30:00Z")
    resets = int(datetime(2026, 10, 12, 9, 30, tzinfo=UTC).timestamp() * 1000)
    assert quota.parse_limits({"config": config}) == [AgentLimit(10080, 37.5, resets)]


# ----------------------------------------------------------- a failed read


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("upstream_error", "Billing service error: HTTP 500"),
        (
            "unreachable",
            "Failed to fetch billing data: error sending request for url (grok.example.com)",
        ),
        ("signed_out", "Authentication required to fetch billing data"),
    ],
)
async def test_a_failed_read_is_grok_s_own_words_with_no_address(case: str, error: str) -> None:
    account = AgentAccount(provider="xai", method="account")

    await quota.fill_limits(account, answering(case))

    assert account.limits is None
    assert account.limits_error == error
    assert account.limits_checked_at is not None


async def test_a_leader_that_does_not_answer_in_time_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quota, "LIMITS_TIMEOUT", 0.05)

    async def hangs() -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    account = AgentAccount(provider="xai", method="account")
    await quota.fill_limits(account, hangs)

    assert account.limits is None
    assert account.limits_error == quota.SLOW_LEADER


async def test_an_unexpected_failure_still_reports_the_account() -> None:
    async def breaks() -> dict[str, Any]:
        raise ValueError("no")

    account = AgentAccount(provider="xai", method="account")
    await quota.fill_limits(account, breaks)

    assert account.limits_error == "could not read the Grok quota: ValueError"


def test_grok_s_words_keep_one_line_and_lose_every_address() -> None:
    said = "Failed: url (https://proxy.example.com/v1/billing?token=secret)\nsecond line"
    assert quota.reason(said) == "Failed: url (proxy.example.com)"
    refused = "connect error: 127.0.0.1:8443, devbox.local:443, localhost:80"
    assert quota.reason(refused) == "connect error: 127.0.0.1, devbox.local, localhost"
    assert quota.reason("   ") == quota.NO_REASON
    assert len(quota.reason("x" * 500)) == quota.MAX_ERROR


# ------------------------------------------------------------- who is asked


async def test_only_a_signed_in_account_is_asked_and_only_for_the_device_agents_reply() -> None:
    asked: list[str] = []

    async def billing() -> dict[str, Any]:
        asked.append("billing")
        return recorded("weekly")

    sign_in({"key": "xai-test", "auth_mode": "api_key"})
    keyed = await grok_account.detect(limits=True, billing=billing)
    sign_in()
    quiet = await grok_account.detect(limits=False, billing=billing)

    assert asked == []
    assert keyed[0].limits is None and quiet[0].limits is None
    assert quiet[0].limits_checked_at is None


# ----------------------------------------------------------- the leader's states


@pytest.mark.parametrize(
    "configuration",
    ["", '[cli]\nuse_leader = true\n[sandbox]\nprofile = "workspace"\n'],
    ids=["leader-mode-off", "sandboxed"],
)
async def test_with_leader_mode_off_nothing_is_asked_and_nothing_is_said(
    configuration: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROK_SANDBOX", raising=False)
    sign_in()
    (grok_runtime.home() / "config.toml").write_text(configuration, encoding="utf-8")
    _, _, service = build(tmp_path)

    accounts = await grok_account.detect(limits=True, billing=service.billing)

    assert [account.to_dict() for account in accounts] == [
        {"provider": "xai", "method": "account", "email": EMAIL}
    ]


async def test_with_leader_mode_on_and_no_link_yet_the_card_says_it_is_not_connected(
    leader_mode: Path, tmp_path: Path
) -> None:
    _, _, service = build(tmp_path)

    accounts = await grok_account.detect(limits=True, billing=service.billing)

    assert accounts[0].limits is None
    assert accounts[0].limits_error == quota.NOT_JOINED
    assert accounts[0].limits_checked_at is not None


async def test_a_link_lost_after_detection_began_says_it_is_not_connected(
    leader_mode: Path, tmp_path: Path
) -> None:
    """Detection is handed the reader while the link is up; it may drop before the read."""
    binary, peer = leader_peer(tmp_path)
    (peer.directory / "billing").write_text("weekly", encoding="utf-8")
    _, _, service = build(tmp_path)
    assert await service.ensure(binary) is True
    reader = service.billing
    await drop_link(service)

    accounts = await grok_account.detect(limits=True, billing=reader)

    assert accounts[0].limits is None
    assert accounts[0].limits_error == quota.NOT_JOINED
    assert peer.calls("_x.ai/billing") == []
    await service.stop()


async def test_the_leader_answers_for_the_account_it_is_signed_in_with(
    leader_mode: Path, tmp_path: Path
) -> None:
    binary, peer = leader_peer(tmp_path)
    (peer.directory / "billing").write_text("weekly", encoding="utf-8")
    _, _, service = build(tmp_path)
    assert await service.ensure(binary) is True

    accounts = await grok_account.detect(limits=True, billing=service.billing)

    assert accounts[0].limits == [AgentLimit(10080, 37.5, WEEK_ENDS)]
    assert accounts[0].limits_error is None
    assert peer.params("_x.ai/billing") == {}
    await service.stop()


async def test_the_leader_s_own_error_reaches_the_account(
    leader_mode: Path, tmp_path: Path
) -> None:
    binary, peer = leader_peer(tmp_path)
    (peer.directory / "billing").write_text("upstream_error", encoding="utf-8")
    _, _, service = build(tmp_path)
    assert await service.ensure(binary) is True

    accounts = await grok_account.detect(limits=True, billing=service.billing)

    assert accounts[0].limits is None
    assert accounts[0].limits_error == "Billing service error: HTTP 500"
    await service.stop()


async def test_a_leader_that_never_answers_costs_the_read_and_not_the_connection(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quota, "LIMITS_TIMEOUT", 0.3)
    binary, peer = leader_peer(tmp_path)
    (peer.directory / "billing").write_text("silent", encoding="utf-8")
    _, _, service = build(tmp_path)
    assert await service.ensure(binary) is True

    slow = await grok_account.detect(limits=True, billing=service.billing)
    (peer.directory / "billing").write_text("weekly_unused", encoding="utf-8")
    later = await grok_account.detect(limits=True, billing=service.billing)

    assert slow[0].limits is None and slow[0].limits_error == quota.SLOW_LEADER
    assert later[0].limits == [AgentLimit(10080, 0, WEEK_ENDS)]
    assert service.ready is True
    await service.stop()


# ---------------------------------------------------------- the scan's join


async def test_the_scan_joins_a_leader_turned_on_after_the_daemon_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROK_SANDBOX", raising=False)
    monkeypatch.setattr(grok_runtime, "probe_version", no_version)
    sign_in()
    binary, _ = leader_peer(tmp_path)
    _, _, service = build(tmp_path)

    await service.tick(binary)
    assert connected(service) is False
    (grok_runtime.home() / "config.toml").write_text("[cli]\nuse_leader = true\n", encoding="utf-8")
    await service.tick(binary)

    assert connected(service) is True
    await service.stop()


async def test_the_scan_joins_once_grok_is_installed(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_runtime, "resolve_binary", lambda: None)
    monkeypatch.setattr(grok_runtime, "probe_version", no_version)
    binary, _ = leader_peer(tmp_path)
    _, _, service = build(tmp_path)

    await service.tick(None)
    assert connected(service) is False
    await service.tick(binary)

    assert connected(service) is True
    await service.stop()


async def test_a_leader_that_cannot_start_is_not_started_on_every_scan(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = tmp_path / "starts"
    broken = tmp_path / "broken-agent"
    broken.write_text(f'#!/bin/sh\necho start >> "{starts}"\nexit 1\n', encoding="utf-8")
    broken.chmod(0o755)
    clock = [1000.0]
    monkeypatch.setattr(grok_service, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _, _, service = build(tmp_path)

    await service.tick(str(broken))
    await service.tick(str(broken))
    clock[0] += grok_service.RETRY_FIRST
    await service.tick(str(broken))
    clock[0] += grok_service.RETRY_FIRST
    await service.tick(str(broken))

    # A try, a wait, a second try, and a wait twice as long as the first.
    assert starts.read_text(encoding="utf-8").split() == ["start", "start"]
    assert service.ready is False
    await service.stop()


async def test_a_join_made_elsewhere_ends_the_scan_s_wait(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session started from an app joins through `ensure`; a later crash is rejoined at once."""
    clock = [1000.0]
    monkeypatch.setattr(grok_service, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(grok_runtime, "probe_version", no_version)
    binary, peer = leader_peer(tmp_path)
    launcher, failing = flaky(tmp_path, binary)
    _, _, service = build(tmp_path)
    await service.tick(launcher)
    assert connected(service) is False
    failing.unlink()
    assert await service.ensure(launcher) is True

    await drop_link(service)
    await service.tick(launcher)

    assert connected(service) is True
    assert len(peer.calls("initialize")) == 2
    await service.stop()


async def test_a_terminal_session_appearing_during_the_wait_is_joined_at_once(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(grok_service, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    binary, _ = leader_peer(tmp_path)
    launcher, failing = flaky(tmp_path, binary)
    hub, _, service = build(tmp_path)
    await service.tick(launcher)
    assert connected(service) is False
    failing.unlink()
    write_summary(leader_mode)
    register(leader_mode)

    await service.tick(launcher)

    assert connected(service) is True
    assert hub.entries[SESSION].session.control == "shared"
    await service.stop()


@pytest.mark.parametrize(
    "configuration",
    ["", '[cli]\nuse_leader = true\n[sandbox]\nprofile = "workspace"\n'],
    ids=["leader-mode-off", "sandboxed"],
)
async def test_leader_mode_off_starts_no_leader_and_on_again_rejoins_what_it_held(
    configuration: str, leader_mode: Path, tmp_path: Path
) -> None:
    """A link lost while leader mode is off stays down; the held session comes back with it."""
    hub, _, service, peer = await shared_session(leader_mode, tmp_path)
    turned_on = (leader_mode / "config.toml").read_text(encoding="utf-8")
    loads = len(peer.calls("session/load"))
    await drop_link(service)
    (leader_mode / "config.toml").write_text(configuration, encoding="utf-8")

    await service.tick(None)
    assert connected(service) is False
    assert service._client is not None
    assert len(peer.calls("initialize")) == 1

    (leader_mode / "config.toml").write_text(turned_on, encoding="utf-8")
    await service.tick(None)

    assert connected(service) is True
    assert len(peer.calls("session/load")) > loads
    sent = await hub.send({"id": str(uuid.uuid4()), "session_id": SESSION, "text": "back again"})
    assert sent["accepted"] == "sent"
    await wait_for(lambda: prompts(peer) == ["back again"], timeout=5)
    await service.stop()


async def test_joins_made_at_once_share_one_leader_client(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app's new session and the scan may both find no link and join together."""
    monkeypatch.setattr(grok_runtime, "probe_version", no_version)
    binary, peer = leader_peer(tmp_path)
    _, _, service = build(tmp_path)

    results = await asyncio.gather(
        service.ensure(binary), service.tick(binary), service.ensure(binary)
    )

    assert results[0] is True and results[2] is True
    assert len(peer.calls("initialize")) == 1
    await service.stop()


async def test_a_rejoin_runs_the_launcher_the_scan_resolves_now(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_runtime, "probe_version", no_version)
    old, old_peer = leader_peer(tmp_path)
    new, new_peer = leader_peer(tmp_path, mode="leader-moved")
    _, _, service = build(tmp_path)
    assert await service.ensure(old) is True

    await drop_link(service)
    await service.tick(new)

    assert connected(service) is True
    assert len(old_peer.calls("initialize")) == 1
    assert len(new_peer.calls("initialize")) == 1
    await service.stop()


# ------------------------------------------------------- the device.agents reply


async def test_the_device_agents_reply_shows_grok_s_window_in_leader_mode_even_at_zero(
    leader_mode: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, peer = leader_peer(tmp_path)
    (peer.directory / "billing").write_text("weekly_unused", encoding="utf-8")
    monkeypatch.setenv("RC_GROK_BIN", binary)

    async def detect_all(context: DetectContext | None = None) -> list[AgentInfo]:
        return [await detect_grok(context or DetectContext())]

    monkeypatch.setattr(grok_runtime, "probe_version", no_version)
    monkeypatch.setattr(daemon_module, "detect_all", detect_all)
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
        joining = await daemon._device_agents({})
        assert await daemon.grok.ensure(binary) is True
        joined = await daemon._device_agents({})
        hello = await daemon._hello()
        quiet = daemon._detect_context()
    finally:
        await daemon.grok.stop()
        daemon.registry.close()

    # Leader mode is on but the device has not joined: the card says why.
    before = joining["agents"][0]["accounts"][0]
    assert before["limits_error"] == "not connected to the Grok leader yet; the device keeps trying"
    assert "limits" not in before
    account = joined["agents"][0]["accounts"][0]
    assert account["limits"] == [
        {"window_minutes": 10080, "used_percent": 0, "resets_at": WEEK_ENDS}
    ]
    assert "limits_error" not in account and isinstance(account["limits_checked_at"], int)
    # Only that reply carries quota, so nothing else asked the leader (A33).
    assert hello["agents"][0]["accounts"] == [
        {"provider": "xai", "method": "account", "email": EMAIL}
    ]
    assert quiet.grok_billing is None
    assert len(peer.calls("_x.ai/billing")) == 1


# ----------------------------------------------------------------- helpers


async def no_version(path: str) -> None:
    """What `--version` says of a fake leader: nothing, so no drift is seen."""
    return None


def prompts(peer: Peer) -> list[str]:
    return [str(call["prompt"][0]["text"]) for call in peer.calls("session/prompt")]


def flaky(tmp_path: Path, binary: str) -> tuple[str, Path]:
    """A launcher that fails while its flag file exists, and is `binary` after."""
    failing = tmp_path / "failing"
    failing.write_text("", encoding="utf-8")
    launcher = tmp_path / "flaky-agent"
    launcher.write_text(
        f'#!/bin/sh\n[ -e "{failing}" ] && exit 1\nexec "{binary}" "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    return str(launcher), failing


async def drop_link(service: GrokLeaderService) -> None:
    """Kill the leader child under the service, as a crashed one would go."""
    client = service._client
    assert client is not None and client._agent is not None
    child = client._agent._process
    assert child is not None
    child.kill()
    await wait_for(lambda: not service.ready, timeout=5)
