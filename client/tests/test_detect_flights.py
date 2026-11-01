"""One detection at a time for an agent, and an old one never replaces a newer one (A33).

An ask for an agent whose detection is already under way, for the same context, waits for that one
instead of starting another, so repeated asks cannot pile probes up behind a slow agent. A
detection that finishes late does not replace the answer of one that started after it.
"""

from __future__ import annotations

import asyncio

import pytest

from rc_client.agents import detection, registry
from rc_client.agents.registry import DetectContext, detect_all
from rc_client.models import AgentInfo
from tests.detect_helpers import after, counting, everyone, install, reporting, settle, versions

WITH_QUOTA = DetectContext(limits=True)


# ------------------------------------------------------------- probes that do not pile up


async def test_asks_made_while_an_agent_is_slow_wait_on_one_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = asyncio.Event()
    asked: list[DetectContext] = []
    install(
        monkeypatch,
        {**everyone("2.0"), "pi": counting(after(gate, reporting("pi", "2.0")), asked)},
    )

    for _ in range(3):
        assert versions(await detect_all(WITH_QUOTA))["pi"] == "1.0"

    assert len(asked) == 1, "a slow probe is not asked again while it is still running"
    gate.set()
    await settle()
    assert versions(await detect_all(WITH_QUOTA))["pi"] == "2.0"
    assert len(asked) == 2, "once it has finished, the next ask starts a new one"


async def test_asks_made_together_share_one_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = asyncio.Event()
    asked: list[DetectContext] = []
    install(
        monkeypatch, {**everyone("1.0"), "pi": counting(after(gate, reporting("pi", "1.0")), asked)}
    )
    asks = [asyncio.ensure_future(detect_all(WITH_QUOTA)) for _ in range(4)]
    await asyncio.sleep(0.05)

    gate.set()
    answers = await asyncio.gather(*asks)

    assert len(asked) == 1
    assert [versions(agents)["pi"] for agents in answers] == ["1.0"] * 4


async def test_a_plain_detection_and_a_request_for_quota_are_different_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    asked: list[DetectContext] = []
    install(
        monkeypatch, {**everyone("1.0"), "pi": counting(after(gate, reporting("pi", "1.0")), asked)}
    )
    plain = asyncio.ensure_future(detect_all())
    quota = asyncio.ensure_future(detect_all(WITH_QUOTA))
    await asyncio.sleep(0.05)

    gate.set()
    await asyncio.gather(plain, quota)

    assert [context.limits for context in asked] == [False, True]


async def test_a_detection_that_began_before_the_shared_daemon_appeared_is_not_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_codex_mode_changed` asks because the context changed: it needs an answer of its own."""
    gate = asyncio.Event()
    asked: list[DetectContext] = []
    install(
        monkeypatch,
        {**everyone("1.0"), "codex": counting(after(gate, reporting("codex", "1.0")), asked)},
    )
    before = asyncio.ensure_future(detect_all(DetectContext(codex_daemon_ready=False)))
    await asyncio.sleep(0.05)
    changed = asyncio.ensure_future(detect_all(DetectContext(codex_daemon_ready=True)))
    await asyncio.sleep(0.05)

    gate.set()
    await asyncio.gather(before, changed)

    assert [context.codex_daemon_ready for context in asked] == [False, True]


# ------------------------------------------------------------ order of answers


async def test_a_detection_that_finishes_late_never_replaces_a_newer_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    gate = asyncio.Event()
    install(monkeypatch, {**everyone("2.0"), "pi": after(gate, reporting("pi", "2.0"))})
    await detect_all(WITH_QUOTA)
    install(monkeypatch, everyone("3.0"))
    await detect_all()

    gate.set()
    await settle()

    assert registry.detections.last_good["pi"].version == "3.0", "the old probe came in last"
    assert registry.detections.last_quota["pi"].version == "2.0", "but it is the newest for quota"


async def test_a_newer_answer_replaces_an_older_one_whichever_way_it_comes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0"))
    await detect_all()
    first, second = asyncio.Event(), asyncio.Event()

    async def detect_first(context: DetectContext) -> AgentInfo:
        await first.wait()
        return AgentInfo(agent="pi", available=True, version="first")

    async def detect_second(context: DetectContext) -> AgentInfo:
        await second.wait()
        return AgentInfo(agent="pi", available=True, version="second")

    install(monkeypatch, {**everyone("2.0"), "pi": detect_first})
    await detect_all(WITH_QUOTA)
    install(monkeypatch, {**everyone("2.0"), "pi": detect_second})
    await detect_all(DetectContext(limits=True, proxy="http://proxy.example:3128"))

    second.set()
    await settle()
    first.set()
    await settle()

    assert registry.detections.last_good["pi"].version == "second"
