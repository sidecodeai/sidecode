"""Only a request for quota, which an app waits for, gives up on a slow agent (A33).

An app waits twenty seconds for `device.agents`. A request for quota gives an agent that has
answered before `AGENT_TIMEOUT` and reports a slower one as it last was. Everything else waits for
the agent, because it must be right rather than quick: the quarter-hour refresh that nobody waits
for, and the shared daemon that appeared and has to be reported as ready.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from rc_client.agents import detection, registry
from rc_client.agents.registry import DetectContext, detect_all
from rc_client.models import AgentInfo
from tests.detect_helpers import after, everyone, install, reporting, settle, versions

LOGGER = "rc_client.agents"
WITH_QUOTA = DetectContext(limits=True)


def warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER]


async def slow_pi(monkeypatch: pytest.MonkeyPatch, version: str = "2.0") -> asyncio.Event:
    """pi answers `version` once the returned gate is opened, and every other agent at once."""
    gate = asyncio.Event()
    install(monkeypatch, {**everyone(version), "pi": after(gate, reporting("pi", version))})
    return gate


# ----------------------------------------------------- a request for quota gives up


async def test_a_slow_agent_that_has_answered_before_is_reported_as_it_last_was(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = await slow_pi(monkeypatch)
    started = time.monotonic()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        agents = await detect_all(WITH_QUOTA)
    elapsed = time.monotonic() - started

    assert elapsed < 2, "the slow agent must not make the reply wait for it"
    assert [info.agent for info in agents] == ["claude", "codex", "grok", "pi"]
    assert versions(agents) == {"claude": "2.0", "codex": "2.0", "grok": "2.0", "pi": "1.0"}
    [record] = warnings(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "agent detection timed out"
    assert record.__dict__["context"]["agent"] == "pi"
    gate.set()
    await settle()


async def test_a_quota_request_gives_an_agent_eight_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waited: list[float | None] = []
    real_wait = asyncio.wait

    async def wait(
        tasks: set[asyncio.Future[AgentInfo]], *, timeout: float | None = None
    ) -> object:
        waited.append(timeout)
        return await real_wait(tasks, timeout=0.01)

    monkeypatch.setattr(asyncio, "wait", wait)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = await slow_pi(monkeypatch)

    await detect_all(WITH_QUOTA)

    assert waited == [8.0] * len(registry.AGENT_IDS), "one wait for each agent, of eight seconds"
    gate.set()
    await settle()


async def test_the_answer_a_slow_agent_gives_late_is_what_it_is_reported_as_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = await slow_pi(monkeypatch)
    await detect_all(WITH_QUOTA)

    gate.set()
    await settle()

    assert registry.detections.last_good["pi"].version == "2.0"
    gate = await slow_pi(monkeypatch, "3.0")
    assert versions(await detect_all(WITH_QUOTA))["pi"] == "2.0"
    gate.set()
    await settle()


async def test_an_agent_that_has_never_answered_is_waited_for(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)

    async def slowly(context: DetectContext) -> AgentInfo:
        await asyncio.sleep(0.15)
        return AgentInfo(agent="pi", available=True, version="1.0")

    install(monkeypatch, {**everyone("1.0"), "pi": slowly})

    with caplog.at_level(logging.INFO, logger=LOGGER):
        agents = await detect_all(WITH_QUOTA)

    assert versions(agents)["pi"] == "1.0"
    assert warnings(caplog) == []


async def test_a_quota_request_reports_an_agent_that_answers_in_time_as_it_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    install(monkeypatch, everyone("2.0"))

    assert versions(await detect_all(WITH_QUOTA)) == dict.fromkeys(registry.AGENT_IDS, "2.0")


# ------------------------------------------------- everything else waits as it did


async def test_a_plain_detection_waits_for_a_slow_agent_and_reports_what_it_found(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()

    async def becomes_ready(context: DetectContext) -> AgentInfo:
        await asyncio.sleep(0.15)
        return AgentInfo(agent="pi", available=True, version="2.0", attach_ready=True)

    install(monkeypatch, {**everyone("2.0"), "pi": becomes_ready})

    with caplog.at_level(logging.INFO, logger=LOGGER):
        agents = await detect_all()

    [pi] = [info for info in agents if info.agent == "pi"]
    assert (pi.version, pi.attach_ready) == ("2.0", True), "a stale answer must never be published"
    assert warnings(caplog) == []


async def test_a_plain_detection_that_waits_for_the_shared_daemon_reports_it_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_codex_mode_changed` asks because the daemon appeared; an old answer would hide that."""
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all(DetectContext(codex_daemon_ready=False))

    async def ready(context: DetectContext) -> AgentInfo:
        await asyncio.sleep(0.15)
        return AgentInfo(
            agent="codex", available=True, attach_ready=bool(context.codex_daemon_ready)
        )

    install(monkeypatch, {**everyone("2.0"), "codex": ready})

    agents = await detect_all(DetectContext(codex_daemon_ready=True))

    assert [info.attach_ready for info in agents if info.agent == "codex"] == [True]


async def test_a_caller_that_stops_waiting_leaves_the_detection_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = await slow_pi(monkeypatch)
    waiting = asyncio.ensure_future(detect_all())
    await asyncio.sleep(0.05)

    waiting.cancel()
    gate.set()
    await settle()

    assert waiting.cancelled(), "cancelling the caller is not swallowed"
    assert registry.detections.last_good["pi"].version == "2.0", "and the detection went on"
