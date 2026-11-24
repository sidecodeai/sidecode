"""A message queued while an `agy` the device did not start may have the conversation open (A64).

The device used to send it into its own `agy acp` the moment its turn ended, with the terminal's
`agy` still there, and to give the conversation up only after that second turn. The presence rules
now decide first: the conversation is given up as the turn ends, the message waits in the queue,
still editable, and goes out once the device has the conversation again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import errors, presence
from rc_client.agents.antigravity.presence import Holder, Holders
from rc_client.agents.antigravity.service import AntigravityService
from rc_client.sessions.hub import SessionEntry
from tests.agy_helpers import SESSION, wait_for
from tests.test_antigravity_exemption import Bench, control, refused, running

QUEUED = "5b0f2a64-8d3e-4c71-9a2b-6e4d8c1f0a37"
EDITED = "9c3e7d21-4b5a-4f86-8e1c-2a7b9d0e6f53"


@pytest.fixture(autouse=True)
def by_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(self: AntigravityService, seconds: float, step: Any) -> None:
        return None

    monkeypatch.setattr(AntigravityService, "_every", never)


def events(bench: Bench, kind: str) -> list[dict[str, Any]]:
    return [
        f["event"]
        for f in bench.frames
        if f.get("type") == "session.event" and f["event"]["kind"] == kind
    ]


def pending(bench: Bench) -> list[dict[str, Any]]:
    return [event for event in events(bench, "approval") if event["status"] == "pending"]


def prompts(bench: Bench) -> list[str]:
    """The words of every `session/prompt` `agy` received, a steer it refused included."""
    return [
        str(block.get("text"))
        for call in bench.peer.calls("session/prompt")
        for block in call.get("prompt") or []
        if block.get("type") == "text"
    ]


def held_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Bench:
    """A device-created session's first prompt raises an approval; `agy`'s list names it."""
    bench = Bench(tmp_path, monkeypatch, turns=["tool"])
    bench.peer.configure(list=bench.listed())

    async def scan(binary: str | None, *, cwds: bool) -> Holders:
        # The real scan waits on `ps` and `lsof`: whatever else is due runs meanwhile.
        await asyncio.sleep(0.05)
        return bench.holders

    monkeypatch.setattr(presence, "scan", scan)
    return bench


async def card_open(bench: Bench) -> SessionEntry:
    await bench.hub.create(
        {"agent": "antigravity", "cwd": str(bench.work), "first_message": "List the files."}
    )
    await wait_for(lambda: pending(bench))
    return bench.entry()


async def queue_one(bench: Bench) -> None:
    queued = await bench.hub.send(
        {"id": QUEUED, "session_id": SESSION, "text": "And then this.", "mode": "auto"}
    )
    assert queued == {"accepted": "queued", "queued_id": QUEUED}


async def deny(bench: Bench, entry: SessionEntry) -> None:
    """Refuse the card, which ends the turn, and wait for the device to give the session up.

    Given up is what the apps are told, `terminal`: the device drops its runner first and says so
    only once its `agy` has exited, which a loaded machine makes take a while.
    """
    assert entry.runner is not None
    await entry.runner.approve(pending(bench)[0]["request_id"], "deny", None)
    await wait_for(lambda: not running(entry) and control(entry) == "terminal")


def naming_it(bench: Bench) -> Holders:
    """`agy --conversation <id>` in a terminal."""
    return Holders([Holder(4242, SESSION, str(bench.work))])


async def test_a_message_queued_as_a_terminal_appears_waits_for_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = held_bench(tmp_path, monkeypatch)
    await bench.start()
    try:
        entry = await card_open(bench)
        bench.holders = naming_it(bench)
        await bench.scan()
        assert [event["text"] for event in events(bench, "notice")] == [errors.ALSO_OPEN]
        await queue_one(bench)
        asked = len(prompts(bench))
        await deny(bench, entry)

        assert len(prompts(bench)) == asked, "nothing went to agy after the turn"
        assert (control(entry), entry.session.state) == ("terminal", "readonly")
        assert [item["id"] for item in entry.queue] == [QUEUED], "the message waits"
        waiting = events(bench, "queue")[-1]["pending"]
        assert [item["id"] for item in waiting] == [QUEUED]

        # Still editable while it waits (A43); anything else is refused.
        await bench.hub.queue_remove({"session_id": SESSION, "queued_id": QUEUED})
        edited = await bench.hub.send(
            {
                "id": EDITED,
                "session_id": SESSION,
                "text": "And then that.",
                "mode": "queue",
                "queue_ts": waiting[0]["ts"],
            }
        )
        assert edited == {"accepted": "queued", "queued_id": EDITED}
        assert (await refused(bench.hub, "More.")).code == "conflict"

        # The terminal's `agy` quits: the device has the conversation again, and the queue goes.
        bench.terminal_gone()
        await bench.scan()
        await wait_for(lambda: "And then that." in prompts(bench))
        await wait_for(lambda: not entry.queue)
        assert control(entry) == "remote"
        assert prompts(bench)[asked:] == ["And then that."]
    finally:
        await bench.stop()


async def test_a_terminal_no_scan_has_seen_yet_takes_it_before_the_queue_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ten-second look came between the terminal's start and the turn's end: the end looks."""
    bench = held_bench(tmp_path, monkeypatch)
    await bench.start()
    try:
        entry = await card_open(bench)
        await queue_one(bench)
        bench.holders = naming_it(bench)
        asked = len(prompts(bench))
        await deny(bench, entry)
        assert len(prompts(bench)) == asked, "nothing went to agy after the turn"
        assert (control(entry), entry.session.state) == ("terminal", "readonly")
        assert [item["id"] for item in entry.queue] == [QUEUED]
        assert events(bench, "notice") == [], "no turn of the device's ran beside the terminal"
    finally:
        await bench.stop()
