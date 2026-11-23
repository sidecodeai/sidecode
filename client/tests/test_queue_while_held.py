"""A64: what is queued for a session a terminal takes waits until the device has it again.

The guard is the hub's and knows no agent: a drain never sends into a session a terminal holds, a
process let go of for a terminal can leave the queue behind, an edit still goes back into it (A43),
and once the terminal lets go of a session the device holds, the queue goes out, the process
started again for it as a message would start it (A58).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.errors import RcError
from rc_client.models import AgentInfo
from rc_client.sessions.hub import SessionEntry
from tests.helpers import drain, drained
from tests.test_hub import FakeRunner, add_session, build_hub

ITEM = {"id": "q-1", "text": "Then run the tests.", "attachments": [], "source": "remote"}


class Built:
    def __init__(self, tmp_path: Path) -> None:
        self.hub, self.frames, self.registry = build_hub(tmp_path)
        self.started: list[FakeRunner] = []

        async def build(entry: SessionEntry, info: AgentInfo, resume: str | None) -> Any:
            runner = FakeRunner(entry.channel)
            self.started.append(runner)
            return runner

        self.hub._build_runner = build  # type: ignore[method-assign]

    def held(self, control: str, *, running: bool) -> SessionEntry:
        """A session the device holds, with one message waiting."""
        entry = add_session(self.hub, control=control)
        entry.session.held = True
        if not running:
            entry.runner = None
        entry.queue.append({**ITEM, "ts": 1})
        return entry

    def queue_events(self) -> list[list[dict[str, Any]]]:
        return [
            frame["event"]["pending"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == "queue"
        ]


@pytest.fixture
def built(tmp_path: Path) -> Any:
    made = Built(tmp_path)
    yield made
    made.registry.close()


def sent(runner: object) -> list[str]:
    assert isinstance(runner, FakeRunner)
    return runner.sent


async def test_a_drain_never_sends_into_a_session_a_terminal_holds(built: Built) -> None:
    entry = built.held("terminal", running=True)
    runner = entry.runner
    await drain(built.hub, entry)
    assert sent(runner) == []
    assert [item["id"] for item in entry.queue] == ["q-1"], "the message waits"


async def test_a_process_let_go_of_for_a_terminal_can_leave_the_queue_behind(
    built: Built,
) -> None:
    entry = built.held("remote", running=True)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert await built.hub.release_process(entry) is False, "an idle release keeps the process"
    assert await built.hub.release_process(entry, keep_queue=True) is True
    assert runner.closed and entry.runner is None
    assert [item["id"] for item in entry.queue] == ["q-1"]


async def test_the_queue_goes_out_once_the_terminal_lets_go(built: Built) -> None:
    entry = built.held("terminal", running=False)
    await built.hub.terminal_holds(entry, False)
    await drained(entry)
    assert entry.session.control == "remote"
    assert len(built.started) == 1, "the process starts again for the queue"
    assert sent(entry.runner) == [ITEM["text"]]
    assert entry.queue == [] and built.queue_events()[-1] == []


async def test_a_session_nobody_holds_is_not_opened_for_its_queue(built: Built) -> None:
    entry = built.held("terminal", running=False)
    entry.session.held = False
    await built.hub.terminal_holds(entry, False)
    await drained(entry)
    assert entry.session.control == "none"
    assert built.started == [] and [item["id"] for item in entry.queue] == ["q-1"]


async def test_an_edit_goes_back_into_the_queue_while_a_terminal_holds_it(
    built: Built,
) -> None:
    entry = built.held("terminal", running=False)
    entry.queue.append({**ITEM, "id": "q-2", "text": "And commit.", "ts": 2})
    await built.hub.queue_remove({"session_id": "sess-1", "queued_id": "q-1"})
    edit = {"id": "q-3", "session_id": "sess-1", "text": "Run the tests.", "mode": "queue"}
    edited = await built.hub.send({**edit, "queue_ts": 1})
    assert edited == {"accepted": "queued", "queued_id": "q-3"}
    assert [item["id"] for item in entry.queue] == ["q-3", "q-2"], "back where it waited"
    assert built.started == [], "nothing opens while the terminal has it"
    for fields in ({"mode": "auto"}, {"mode": "queue"}):
        with pytest.raises(RcError) as caught:
            await built.hub.send({"session_id": "sess-1", "text": "More.", **fields})
        assert caught.value.code == "conflict", "only an edit is taken"


async def test_a_session_with_its_own_process_drains_as_before(built: Built) -> None:
    entry = built.held("remote", running=True)
    await drain(built.hub, entry)
    assert sent(entry.runner) == [ITEM["text"]] and built.started == []
