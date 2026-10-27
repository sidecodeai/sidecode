"""The subagents the CLI's own hooks say are at work (amendment A51).

`SubagentStart` and `SubagentStop` name each subagent by its id, in the hook
payload and nowhere else, so an attached terminal session knows the moment one
begins and the moment it ends, whatever its transcript shows and whether or not
the turn that started it is still open. That is the whole account of "working
means all of it" for a CLI that runs the device's hooks: every subagent heard
starting and not yet heard ending keeps the session shown as working.

A subagent does not always end with a `SubagentStop`. An API error ends one
with `StopFailure`, which the hook reports as the same end; one the person
aborts ends with neither. A hook that never came would otherwise hold a session
for good, so the account is bounded three ways. A subagent that has shown no
sign of life for `AGENT_SILENCE` is let go, the thirty minutes a silent
subagent's transcript is believed: a sign of life is its start or a write to
its transcript, so one that works for hours stays, and one whose end was lost
goes half an hour after its last word; the time it waited for the person's
answer to a prompt of its own is no silence (`waited`). A `Stop` whose
task list holds no agent at all says nothing is running. A turn the person's
Escape ended took the subagents it was waiting for with it, the ones that began
after the last prompt or tool batch the CLI reported (`abort`), and left the
ones that run in the background, which a batch that has finished began and an
Escape does not reach; the vendor's usage limit refuses every request, theirs
too, and takes them all. And the last subagent to end leaves the session shown
as working for `AGENT_HANDOFF` more: the CLI wakes for the end of a background
subagent with a prompt of its own, a moment later, and a turn that ended in
between would be announced as finished and begun again.

An end heard for an id never heard starting — an internal agent of Claude
Code's own, one that began before the daemon did — changes nothing, and neither
does a sign of life from one. A late end of one already let go hands nothing over.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..agents.claude.subagents import SUBAGENT_STALE_S

# How long a subagent heard starting is believed to run with no sign of life.
AGENT_SILENCE = SUBAGENT_STALE_S
# The longest wait for the person's answer that is not held against a subagent: a card left for
# hours says nothing of an agent that may long have died, so no more than this is given back.
WAIT_CREDIT = 2 * AGENT_SILENCE
# How long the session stays shown as working after a subagent stopped, which is long
# enough for the prompt the CLI wakes with to arrive, and short enough to go unseen.
AGENT_HANDOFF = 5.0
# How many subagents are remembered at once; the oldest are forgotten first.
MAX_AGENTS = 256


@dataclass(slots=True)
class Agent:
    """A subagent heard starting: when it began and when it last showed life, both monotonic."""

    began: float
    alive: float

    def at_work(self, now: float) -> bool:
        """Whether it is believed to run: it has shown a sign of life within `AGENT_SILENCE`."""
        return now - self.alive < AGENT_SILENCE


@dataclass(slots=True)
class AgentsAtWork:
    """Which subagents were heard starting and not ending, oldest first."""

    seen: dict[str, Agent] = field(default_factory=dict)
    stopped_at: float | None = None

    def start(self, agent_id: str) -> None:
        """A subagent began, or was resumed; the oldest go first past `MAX_AGENTS`."""
        now = time.monotonic()
        self.seen.pop(agent_id, None)
        self.seen[agent_id] = Agent(began=now, alive=now)
        for forgotten in list(self.seen)[:-MAX_AGENTS]:
            del self.seen[forgotten]

    def touch(self, agent_id: str, wall_time: float) -> None:
        """A sign of life from a subagent heard starting: its transcript written at `wall_time`.

        `wall_time` is in seconds since the epoch. One never heard starting is
        none of ours, and a sign older than the last one says nothing new.
        """
        agent = self.seen.get(agent_id)
        if agent is None:
            return
        now = time.monotonic()
        agent.alive = max(agent.alive, now - max(0.0, time.time() - wall_time))

    def waited(self, seconds: float) -> None:
        """The subagents waited `seconds` for the person's answer, which is no silence of theirs.

        Those believed at work when the prompt was raised are believed so for
        as long as they had been silent before it, and no others: one let go
        before it stays let go. A wait of more than `WAIT_CREDIT` is not given
        back in full, so an answer to a card hours old revives nobody.
        """
        now = time.monotonic()
        credit = min(seconds, WAIT_CREDIT)
        for agent in self.seen.values():
            if now - seconds - agent.alive < AGENT_SILENCE:
                agent.alive = min(now, agent.alive + credit)

    def stop(self, agent_id: str) -> bool:
        """A subagent ended; whether this had heard it start, which is all that counts.

        One already let go for its silence is over for the session too, and its
        late end leaves nothing to hand over to a prompt.
        """
        agent = self.seen.pop(agent_id, None)
        if agent is None:
            return False
        now = time.monotonic()
        if agent.at_work(now):
            self.stopped_at = now
        return True

    def clear(self) -> None:
        """Nothing is running: the CLI listed no agent, or a turn or the person ended them."""
        self.seen.clear()
        self.stopped_at = None

    def abort(self, since: float | None) -> None:
        """The person's Escape aborted the subagents that began at or after `since`, unheard.

        An Escape reaches the subagents the turn waits for, which begin after
        the last prompt or tool batch the CLI reported; those that began before
        it run in the background, on their own, and go on. With no `since` there
        is nothing to tell them by, and none is spared.
        """
        if since is None:
            self.clear()
            return
        for agent_id in [name for name, agent in self.seen.items() if agent.began >= since]:
            del self.seen[agent_id]

    @property
    def running(self) -> bool:
        """Whether a subagent is taken to be at work, or ended a moment ago."""
        now = time.monotonic()
        if any(agent.at_work(now) for agent in self.seen.values()):
            return True
        return self.stopped_at is not None and now - self.stopped_at < AGENT_HANDOFF
