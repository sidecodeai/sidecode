"""How `detect_all` asks one agent, and what it does when the agent is slow or broken (A33).

An app waits for the reply to `device.agents`, and nobody waits for the rest: the quarter-hour
refresh and a shared daemon that appeared are what the device does for itself, and they must be
right rather than quick. So only a request for quota, which is the apps', gives up on an agent. One
that has answered before and takes longer than `AGENT_TIMEOUT` is answered with what it last said
(`slow_agent`), and its detection is left to finish, not cut off half way, for the next ask.
Anything else waits for the agent, as it always did.

An agent whose detection is already under way for the same context is waited for instead of being
asked again, so repeated asks cannot pile probes up behind a slow one, and a detection that
finishes late never replaces the answer of one that started after it. An agent that fails is
answered with what it last said, or left out, and never takes the reply down with it.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from ..logging_setup import logger
from ..models import AgentInfo, now_ms
from .slow_agent import FAILED_AGENT, SLOW_AGENT, remember, stand_in

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .registry import DetectContext

log = logger("rc_client.agents")

# How long a request for quota waits for an agent that has answered before. An app waits twenty
# seconds for `device.agents`, and one slow probe (pi's `--version` and `--list-models`, a Keychain
# that does not answer) must not cost it the agents that did answer.
AGENT_TIMEOUT = 8.0

Ask = Callable[[], Awaitable[AgentInfo]]


@dataclass(slots=True)
class Flight:
    """A detection under way: the context it answers, and where it started among the asks."""

    context: DetectContext
    task: asyncio.Future[AgentInfo]
    order: int


class Detections:
    """What the device knows of its agents between two detections.

    `last_good` is what each agent last answered, without quota windows. `last_quota` is what it
    last said of each account's quota, windows included and kept account by account (`remember`):
    a plain detection in between does not take them away, nor does an answer that has only an
    error for the account.
    """

    def __init__(self) -> None:
        self.last_good: dict[str, AgentInfo] = {}
        self.last_quota: dict[str, AgentInfo] = {}
        self._newest: dict[tuple[str, bool], int] = {}
        self._flights: dict[str, list[Flight]] = {}
        self._orders = itertools.count(1)

    async def detect(self, agent: str, context: DetectContext, ask: Ask) -> AgentInfo | None:
        """`agent`'s answer for `context`, what stands in for it, or nothing when it has none."""
        flight = self._flight(agent, context, ask)
        if context.limits and agent in self.last_good:
            await asyncio.wait({flight.task}, timeout=AGENT_TIMEOUT)
            if not flight.task.done():
                log.warning("agent detection timed out", agent=agent, after=AGENT_TIMEOUT)
                return stand_in(
                    self.last_good[agent], self.last_quota.get(agent), now_ms(), SLOW_AGENT
                )
        try:
            # Shielded: a caller that stops waiting must not cancel the detection others wait on.
            return await asyncio.shield(flight.task)
        except Exception:  # the flight has said so in the log
            return self._failed(agent, context)

    def _failed(self, agent: str, context: DetectContext) -> AgentInfo | None:
        previous = self.last_good.get(agent)
        if previous is None:
            return None
        if not context.limits:
            return previous
        return stand_in(previous, self.last_quota.get(agent), now_ms(), FAILED_AGENT)

    def _flight(self, agent: str, context: DetectContext, ask: Ask) -> Flight:
        for flight in self._flights.get(agent, ()):
            if flight.context == context:
                return flight
        flight = Flight(context, asyncio.ensure_future(_run(ask)), next(self._orders))
        self._flights.setdefault(agent, []).append(flight)
        flight.task.add_done_callback(partial(self._finished, agent, flight))
        return flight

    def _finished(self, agent: str, flight: Flight, task: asyncio.Future[AgentInfo]) -> None:
        flights = self._flights[agent]
        flights.remove(flight)
        if not flights:
            del self._flights[agent]
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            log.warning("agent detection failed", agent=agent, error=type(failure).__name__)
            return
        self._keep(agent, flight, task.result())

    def _keep(self, agent: str, flight: Flight, info: AgentInfo) -> None:
        """Remember an answer, unless one that started after it already is remembered."""
        if flight.order > self._newest.get((agent, False), 0):
            self._newest[agent, False] = flight.order
            self.last_good[agent] = info.without_limits()
        if flight.context.limits and flight.order > self._newest.get((agent, True), 0):
            self._newest[agent, True] = flight.order
            self.last_quota[agent] = remember(self.last_quota.get(agent), info)


async def _run(ask: Ask) -> AgentInfo:
    # Asked from inside the task, so that a plugin that cannot even be imported fails the
    # detection like any other failure, and not the caller.
    return await ask()
