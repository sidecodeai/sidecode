"""Whatever order the hooks and the transcript reads arrive in, the apps' turns stay paired.

The subagent account takes its news from two places that do not agree on an order, the CLI's
hooks and the transcript reads, and each can arrive before, after or between the other's.
Whatever comes, a session shows at most one turn at a time, begins none it does not end
afterwards, and raises nothing.
"""

from __future__ import annotations

import contextlib
import random
from pathlib import Path
from typing import Any

import pytest

from rc_client.errors import RcError
from rc_client.sessions.limits import TurnEnd
from tests.helpers import tick

from .test_shared_agents import LIMIT, hook
from .test_shared_control import FakeAttachment
from .test_typist import Harness

STEPS = 60
STATES = {"idle", "running", "needs_approval", "needs_input", "readonly"}
AGENT_IDS = ["a1", "a2", "a3"]


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


async def step(harness: Harness, entry: Any, rng: random.Random) -> None:
    """One hook, one transcript read, one registration or one Stop from an app, at random."""
    choice = rng.choice(
        ["prompt", "batch", "stop", "start", "end", "tick", "tickend", "again", "appstop"]
    )
    if choice == "prompt":
        await hook(harness, "prompt", rng.choice(["person", "agent", "command", "ours"]))
    elif choice == "batch":
        await hook(harness, "batch")
    elif choice == "stop":
        listed = rng.choice([None, 0, 1])
        await hook(harness, "stop", background=rng.choice([0, 1, 2]), listed=listed)
    elif choice == "start":
        await hook(harness, "agent_start", agent_id=rng.choice(AGENT_IDS))
    elif choice == "end":
        await hook(harness, "agent_stop", agent_id=rng.choice(AGENT_IDS))
    elif choice == "tick":
        await tick(harness.hub, entry, running=rng.choice([True, False]))
    elif choice == "tickend":
        end = rng.choice([TurnEnd(), TurnEnd("interrupted"), TurnEnd("error", LIMIT)])
        await tick(harness.hub, entry, running=False, end=end)
    elif choice == "again":
        again = FakeAttachment()
        again.pid = rng.choice([harness.attachment.pid, 777])
        await harness.hub.attach_registered(again)
        harness.attachment = again
    else:
        with contextlib.suppress(RcError):
            await harness.hub.stop({"session_id": "sess-1"})


@pytest.mark.parametrize("seed", range(120))
async def test_the_turns_the_apps_see_stay_paired_in_any_order(harness: Harness, seed: int) -> None:
    rng = random.Random(seed)
    entry = await harness.attach()
    for number in range(STEPS):
        await step(harness, entry, rng)
        begun = len(harness.events("turn_started"))
        finished = len(harness.events("turn_completed"))
        assert 0 <= begun - finished <= 1, (seed, number, begun, finished)
        assert entry.session.state in STATES, (seed, number, entry.session.state)
