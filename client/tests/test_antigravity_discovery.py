"""What the device reports for Antigravity, and the account's quota from `agy`'s own query (A64)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import catalog, plugin, runtime, usage
from rc_client.agents.antigravity.options import FALLBACK_MODES, Running
from rc_client.agents.registry import DetectContext
from rc_client.models import Choice
from tests.agy_helpers import fake_agy
from tests.helpers import load_fixture
from tests.test_protocol_fixtures import OPTIONAL_AGENT_FIELDS, assert_round_trip

QUOTA = DetectContext(limits=True)
PLAIN = DetectContext()


def usage_answer(**fractions: float) -> dict[str, Any]:
    """`agy`'s usage answer, its groups and windows as the fixture's account shows them."""
    return {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Models within this group: Gemini 4 Neon, Gemini 4 Carbon",
                        "buckets": [
                            {
                                "window": "weekly",
                                "remaining_fraction": fractions.get("weekly", 0.875),
                                "reset_time": "2026-09-19T17:00:00Z",
                            },
                            {
                                "window": "5h",
                                "remaining_fraction": fractions.get("five", 0.75),
                                "reset_time": "2026-09-15T14:20:00Z",
                            },
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "description": "Models within this group: GPT-OSS 120B",
                        "buckets": [
                            {
                                "window": "5h",
                                "remaining_fraction": 1,
                                "reset_time": "2026-09-15T16:00:00Z",
                            },
                            {"window": "weekly", "remaining_fraction": 1},
                            {"window": "monthly", "remaining_fraction": 0.5},
                        ],
                    },
                ]
            },
        },
    }


def queries(peer: Any) -> int:
    return sum(1 for start in peer.starts() if start["argv"][:2] == ["-p", "/usage"])


@pytest.fixture
def agy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Any]:
    binary, peer = fake_agy(tmp_path, usage=usage_answer())
    monkeypatch.setenv("RC_ANTIGRAVITY_BIN", binary)
    return binary, peer


def test_the_binary_is_looked_for_where_the_contract_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit, _ = fake_agy(tmp_path / "explicit")
    on_path, _ = fake_agy(tmp_path / "on-path")
    home = tmp_path / "home"
    local = home / ".local" / "bin" / "agy"
    local.parent.mkdir(parents=True)
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", str(Path(on_path).parent))
    monkeypatch.setenv("RC_ANTIGRAVITY_BIN", explicit)
    assert runtime.resolve_binary() == explicit
    monkeypatch.delenv("RC_ANTIGRAVITY_BIN")
    assert runtime.resolve_binary() == on_path
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    assert runtime.resolve_binary() == str(local)


async def test_an_agy_that_is_not_there_is_reported_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    info = await plugin.detect(QUOTA)
    assert (info.available, info.path, info.version, info.accounts) == (False, None, None, None)
    assert info.models == [] and info.default_model is None
    assert [choice.id for choice in info.permission_modes] == [m.id for m in FALLBACK_MODES]
    assert [choice.id for choice in info.efforts] == ["low", "medium", "high", "xhigh"]
    assert info.capabilities == [
        "worktree",
        "interrupt",
        "queue",
        "steer",
        "attachments",
        "effort",
        "history",
        "commands",
    ]
    assert info.attach is None


async def test_hello_carries_no_account_and_runs_no_query(agy: tuple[str, Any]) -> None:
    binary, peer = agy
    info = await plugin.detect(PLAIN)
    assert (info.available, info.path, info.version) == (True, binary, "1.5.14")
    assert info.accounts is None
    assert "accounts" not in info.to_dict()
    assert queries(peer) == 0


async def test_an_agy_too_old_is_still_available_with_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = fake_agy(tmp_path, version="1.5.9")
    monkeypatch.setenv("RC_ANTIGRAVITY_BIN", binary)
    info = await plugin.detect(PLAIN)
    assert (info.available, info.version) == (True, "1.5.9")


async def test_a_device_agents_reply_matches_the_worked_example(
    agy: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog.remember(
        Running(
            models=[
                Choice("gemini-4-neon", "Gemini 4 Neon"),
                Choice("gemini-4-carbon", "Gemini 4 Carbon"),
                Choice("gemini-3.8-flash", "Gemini 3.8 Flash"),
            ],
            model="gemini-4-neon",
            levels=("low", "medium", "high", "xhigh"),
            effort="high",
            modes=list(FALLBACK_MODES),
            mode="default",
        )
    )
    monkeypatch.setattr(usage, "now_ms", lambda: 1789470000000)
    info = await plugin.detect(QUOTA)
    produced = info.to_dict()
    produced["path"] = "/Users/me/.local/bin/agy"
    assert_round_trip(
        produced, load_fixture("objects/agent.antigravity.json"), OPTIONAL_AGENT_FIELDS
    )


async def test_a_reading_is_held_a_minute_and_never_taken_on_a_timer(
    agy: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, peer = agy
    clock = [1000.0]
    plugin.SERVICE.usage = usage.Usage(clock=lambda: clock[0])
    await plugin.detect(QUOTA)
    await plugin.detect(QUOTA)
    assert queries(peer) == 1, "apps asking together cost one run"
    clock[0] += usage.HOLD + 1
    await plugin.detect(QUOTA)
    assert queries(peer) == 2
    await plugin.detect(PLAIN)
    assert queries(peer) == 2, "only a device.agents reply runs it"


async def test_a_failed_run_is_stood_in_for_by_the_last_good_reading(
    agy: tuple[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, peer = agy
    clock = [1000.0]
    plugin.SERVICE.usage = usage.Usage(clock=lambda: clock[0])
    now = [1789470000000]
    monkeypatch.setattr(usage, "now_ms", lambda: now[0])
    good = (await plugin.detect(QUOTA)).accounts
    assert good and good[0].limits
    peer.configure(usage_fails=True)
    clock[0] += usage.HOLD + 1
    now[0] += 10 * 60 * 1000
    [account] = (await plugin.detect(QUOTA)).accounts or []
    assert account.limits == good[0].limits
    assert account.limits_checked_at == 1789470000000, "when that reading was made"
    assert account.limits_error is None
    # Half an hour on, nothing may stand in: the device says why there is no meter.
    now[0] += 30 * 60 * 1000
    clock[0] += usage.HOLD + 1
    [account] = (await plugin.detect(QUOTA)).accounts or []
    assert account.limits is None
    assert account.limits_error == usage.FAILED


def test_windows_come_in_agy_s_order_and_an_unused_one_has_no_reset() -> None:
    reading = usage.Reading(usage.parse(usage_answer()), 0)
    limits = [limit.to_dict() for limit in usage.limits(reading)]
    assert limits == [
        {
            "window_minutes": 300,
            "used_percent": 25,
            "scope": "Gemini Models",
            "resets_at": 1789482000000,
        },
        {
            "window_minutes": 10080,
            "used_percent": 12.5,
            "scope": "Gemini Models",
            "resets_at": 1789837200000,
        },
        {"window_minutes": 300, "used_percent": 0, "scope": "Claude and GPT models"},
        {"window_minutes": 10080, "used_percent": 0, "scope": "Claude and GPT models"},
    ]


def test_an_answer_of_another_shape_is_unreadable() -> None:
    with pytest.raises(usage.UsageFailed) as caught:
        usage.parse(json.loads('{"response": "Gemini Models\\t100%"}'))
    assert caught.value.reason == usage.UNREADABLE


async def test_the_query_runs_in_the_device_s_own_empty_folder(agy: tuple[str, Any]) -> None:
    binary, peer = agy
    await usage.query(binary)
    [start] = [s for s in peer.starts() if s["argv"][:1] == ["-p"]]
    assert start["argv"] == ["-p", "/usage", "--output-format", "json"]
    assert Path(start["cwd"]).resolve() == runtime.lists_dir().resolve()
