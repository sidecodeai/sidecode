"""One agent that fails answers for itself and never takes the reply of the others down (A33).

An agent that fails is reported as it last was, a request for quota says that the device could not
check it where it normally carries quota, and one that never answered is left out of the list. The
rest of the agents answer as they always did.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.agents.registry import AGENT_IDS, DetectContext, detect_all
from rc_client.agents.slow_agent import FAILED_AGENT
from rc_client.errors import RcError
from rc_client.models import AgentAccount, AgentLimit
from tests.detect_helpers import after, everyone, install, raising, reporting, settle, versions
from tests.usage_helpers import READ_AT, Clock

LOGGER = "rc_client.agents"
WITH_QUOTA = DetectContext(limits=True)
BROKEN = RuntimeError("the adapter is broken")


def failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == LOGGER and r.getMessage() == "agent detection failed"
    ]


def subscription(*windows: AgentLimit) -> AgentAccount:
    return AgentAccount(
        provider="anthropic",
        method="account",
        email="me@example.com",
        limits=list(windows),
        limits_checked_at=READ_AT,
    )


# ------------------------------------------------------- what a failure is answered with


async def test_an_agent_that_fails_is_reported_as_it_last_was_and_the_others_still_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    install(monkeypatch, {**everyone("2.0"), "pi": raising(BROKEN)})

    with caplog.at_level(logging.INFO, logger=LOGGER):
        agents = await detect_all()

    assert [info.agent for info in agents] == list(AGENT_IDS)
    assert versions(agents) == {"claude": "2.0", "codex": "2.0", "grok": "2.0", "pi": "1.0"}
    [record] = failures(caplog)
    assert record.levelno == logging.WARNING
    assert record.__dict__["context"] == {"agent": "pi", "error": "RuntimeError"}


async def test_an_agent_that_never_answered_and_fails_is_left_out_and_the_others_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, {**everyone("1.0"), "codex": raising(BROKEN)})

    agents = await detect_all()

    assert [info.agent for info in agents] == ["claude", "grok", "pi"]


async def test_a_plugin_that_cannot_be_imported_fails_that_agent_alone(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, everyone("1.0"))
    real = registry.plugin

    def plugin(agent: str) -> Any:
        if agent == "grok":
            raise ImportError("a module the adapter needs is missing")
        return real(agent)

    monkeypatch.setattr(registry, "plugin", plugin)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        agents = await detect_all()

    assert [info.agent for info in agents] == ["claude", "codex", "pi"]
    [record] = failures(caplog)
    assert record.__dict__["context"] == {"agent": "grok", "error": "ImportError"}


async def test_an_error_the_agent_raises_on_purpose_is_a_failure_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch, {**everyone("1.0"), "pi": raising(RcError("agent_unavailable", "pi is gone"))}
    )

    assert [info.agent for info in await detect_all()] == ["claude", "codex", "grok"]


async def test_a_failure_is_logged_once_however_many_wait_for_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = asyncio.Event()
    install(monkeypatch, {**everyone("2.0"), "pi": after(gate, raising(BROKEN))})
    asks = [asyncio.ensure_future(detect_all()) for _ in range(3)]
    await asyncio.sleep(0.05)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        gate.set()
        answers = await asyncio.gather(*asks)

    assert [versions(agents)["pi"] for agents in answers] == ["1.0"] * 3
    assert len(failures(caplog)) == 1


async def test_an_agent_that_fails_after_it_was_reported_slow_is_logged_not_lost(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from rc_client.agents import detection

    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = asyncio.Event()
    install(monkeypatch, {**everyone("2.0"), "pi": after(gate, raising(BROKEN))})
    await detect_all(WITH_QUOTA)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        gate.set()
        await settle()

    [record] = failures(caplog)
    assert record.__dict__["context"] == {"agent": "pi", "error": "RuntimeError"}
    assert registry.detections.last_good["pi"].version == "1.0"


# ------------------------------------------------------- a request for quota that fails


async def test_a_request_for_quota_shows_the_windows_an_agent_that_fails_last_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = AgentLimit(300, 16, resets_at=READ_AT + 3_600_000)
    monkeypatch.setattr("rc_client.agents.detection.now_ms", Clock().now_ms)
    install(monkeypatch, everyone("1.0", pi=[subscription(window)]))
    await detect_all(WITH_QUOTA)
    install(monkeypatch, {**everyone("2.0"), "pi": raising(BROKEN)})

    pi = (await detect_all(WITH_QUOTA))[3]

    assert pi.accounts is not None
    [account] = pi.accounts
    assert account.limits == [window]
    assert account.limits_error is None
    assert account.limits_checked_at == READ_AT


async def test_a_request_for_quota_says_the_device_could_not_check_an_agent_that_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, everyone("1.0", pi=[subscription()]))
    await detect_all()
    install(monkeypatch, {**everyone("2.0"), "pi": raising(BROKEN)})

    pi = (await detect_all(WITH_QUOTA))[3]

    assert pi.accounts is not None
    [account] = pi.accounts
    assert account.limits is None
    assert account.limits_error == FAILED_AGENT
    assert "slow" not in FAILED_AGENT, "it did not run out of time; it failed"


async def test_a_plain_detection_of_an_agent_that_fails_carries_no_line_about_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, everyone("1.0", pi=[subscription()]))
    await detect_all()
    install(monkeypatch, {**everyone("2.0"), "pi": raising(BROKEN)})

    pi = (await detect_all())[3]

    assert pi.accounts is not None and pi.accounts[0].limits_error is None


async def test_a_detection_that_cannot_say_anything_leaves_the_reply_of_the_others_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        {agent: raising(BROKEN) for agent in AGENT_IDS} | {"claude": reporting("claude", "1.0")},
    )

    assert [info.agent for info in await detect_all(WITH_QUOTA)] == ["claude"]
