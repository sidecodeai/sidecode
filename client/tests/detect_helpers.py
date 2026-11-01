"""Stand-ins for the agents `detect_all` asks, which a test can make slow (A33)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from rc_client.agents import registry
from rc_client.agents.registry import AGENT_IDS, DetectContext
from rc_client.models import AgentAccount, AgentInfo

Detector = Callable[[DetectContext], Awaitable[AgentInfo]]


def reporting(agent: str, version: str, accounts: list[AgentAccount] | None = None) -> Detector:
    """A detection that answers at once; a plain one carries no quota windows, as the real ones."""

    async def detect(context: DetectContext) -> AgentInfo:
        answered = accounts if context.limits or accounts is None else _plain(accounts)
        return AgentInfo(agent=agent, available=True, version=version, accounts=answered)

    return detect


def _plain(accounts: list[AgentAccount]) -> list[AgentAccount]:
    return [account.without_limits() for account in accounts]


def raising(failure: Exception) -> Detector:
    """A detection that fails with `failure`."""

    async def detect(context: DetectContext) -> AgentInfo:
        raise failure

    return detect


def counting(answer: Detector, asked: list[DetectContext]) -> Detector:
    """`answer`, with every context it is asked for noted down."""

    async def detect(context: DetectContext) -> AgentInfo:
        asked.append(context)
        return await answer(context)

    return detect


def everyone(version: str, **accounts: list[AgentAccount]) -> dict[str, Detector]:
    return {agent: reporting(agent, version, accounts.get(agent)) for agent in AGENT_IDS}


def install(monkeypatch: pytest.MonkeyPatch, detectors: dict[str, Detector]) -> None:
    plugins = {agent: SimpleNamespace(AGENT=agent, detect=detectors[agent]) for agent in AGENT_IDS}
    monkeypatch.setattr(registry, "plugin", plugins.__getitem__)


def after(gate: asyncio.Event, answer: Detector) -> Detector:
    """A detection that does not start to answer until the test opens the gate."""

    async def detect(context: DetectContext) -> AgentInfo:
        await gate.wait()
        return await answer(context)

    return detect


def versions(agents: list[AgentInfo]) -> dict[str, str | None]:
    return {info.agent: info.version for info in agents}


async def settle() -> None:
    """Let the tasks a test released run to their end."""
    await asyncio.sleep(0.02)
