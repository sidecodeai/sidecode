"""The agents a run starts in the background, each on its own, and what waits for one.

The gateway link waits for none of them (`Daemon.run`): reaching the shared Codex
daemon, joining the Grok leader and asking every agent's CLI what it offers took
minutes on a loaded machine. Each agent starts apart from the others. Claude and
pi are asked what they offer at once; Codex once its shared daemon has been
tried, since its answer says whether the daemon is there (A11); Grok once its
leader has been tried (A28). A request that acts through an agent waits for that
agent alone, so a slow Codex never holds back a Claude reply.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from ..logging_setup import logger
from ..models import AgentInfo
from .registry import AGENT_IDS

log = logger("rc_client.agents")

Step = Callable[[], Awaitable[object]]
Detect = Callable[[str], Awaitable[AgentInfo | None]]
Found = Callable[[str, AgentInfo | None], None]
Changed = Callable[[], Awaitable[None]]


class AgentStartup:
    """Starts every agent once, and says which have started."""

    def __init__(
        self,
        detect: Detect,
        found: Found,
        changed: Changed,
        before: dict[str, Step],
        after: dict[str, Step],
    ) -> None:
        self._detect = detect
        # Told each agent's answer before anything waiting for the agent runs.
        self._found = found
        self._changed = changed
        # What an agent needs before it is asked: Codex its daemon, Grok its leader.
        self._before = before
        # What follows an agent's start, and nothing waits for.
        self._after = after
        self._started = {agent: asyncio.Event() for agent in AGENT_IDS}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="agents")

    async def stop(self) -> None:
        """Stop starting agents, and let go of whatever waits for one: the device is stopping."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for event in self._started.values():
            event.set()

    def needs_service(self, agent: str) -> bool:
        """Whether `agent` starts by reaching a service: Codex its daemon, Grok its leader."""
        return agent in self._before

    def started(self, agent: str) -> bool:
        """Whether `agent` has started, or nothing is starting it."""
        event = self._started.get(agent)
        return self._task is None or event is None or event.is_set()

    @property
    def done(self) -> bool:
        return all(self.started(agent) for agent in AGENT_IDS)

    async def wait(self, agent: str, timeout: float) -> bool:
        """Whether `agent` has started, waiting up to `timeout` for it."""
        event = self._started.get(agent)
        if self.started(agent) or event is None:
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)
        return event.is_set()

    async def wait_all(self, timeout: float) -> None:
        """Wait up to `timeout` for every agent."""
        if self.done:
            return
        events = [event.wait() for event in self._started.values()]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*events), timeout)

    async def _run(self) -> None:
        await asyncio.gather(*(self._start(agent) for agent in AGENT_IDS))

    async def _start(self, agent: str) -> None:
        info: AgentInfo | None = None
        try:
            await self._prepare(agent)
            info = await self._detect(agent)
        except Exception:
            log.exception("asking an agent what it offers failed", agent=agent)
        self._found(agent, info)
        self._started[agent].set()
        try:
            await self._changed()
            after = self._after.get(agent)
            if after is not None:
                await after()
        except Exception:
            log.exception("what follows an agent's start failed", agent=agent)

    async def _prepare(self, agent: str) -> None:
        """Reach the service the agent needs; failing to is not fatal, and detection says so."""
        before = self._before.get(agent)
        if before is None:
            return
        try:
            await before()
        except Exception as exc:
            log.warning("an agent's service could not be reached", agent=agent, error=repr(exc))
