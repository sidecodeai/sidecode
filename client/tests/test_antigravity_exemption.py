"""Rule 3 of 4.4 belongs to sessions the device created and has held ever since (A64, A58).

The hold A58 gives a conversation a message reached — a hand-off from `agy`'s list, a message a
holder refused, a session continued after the person closed it — never exempts it from rules 4 and
5: an `agy` in its directory holds it on every send.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents import registry as agents
from rc_client.agents.antigravity import errors, exemption, plugin, presence
from rc_client.agents.antigravity.presence import Holder, Holders
from rc_client.agents.antigravity.service import AntigravityService
from rc_client.config import MirrorConfig
from rc_client.errors import RcError
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.agy_helpers import SESSION, agent_info, fake_agy, wait_for

UPDATED = "2026-11-22T11:19:33Z"


class Bench:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **config: Any) -> None:
        self.work = tmp_path / "project"
        self.work.mkdir()
        self.binary, self.peer = fake_agy(tmp_path, **config)
        monkeypatch.setenv("RC_ANTIGRAVITY_BIN", self.binary)
        self.registry = Registry(tmp_path / "state.sqlite3")
        self.frames: list[dict[str, Any]] = []

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", lambda: [agent_info(self.binary)])
        # Who runs `agy` where, as the test says: nobody until it puts a terminal somewhere.
        self.holders = Holders()

        async def scan(binary: str | None, *, cwds: bool) -> Holders:
            return self.holders

        monkeypatch.setattr(presence, "scan", scan)
        agents.plugin("antigravity")

    def listed(self) -> list[dict[str, Any]]:
        return [{"sessionId": SESSION, "cwd": str(self.work), "title": "t", "updatedAt": UPDATED}]

    def terminal_in_the_project(self) -> None:
        self.holders = Holders([Holder(4242, None, str(self.work))])

    def terminal_gone(self) -> None:
        self.holders = Holders()

    async def start(self) -> None:
        async def refresh() -> None:
            return None

        await plugin.SERVICE.start(self.hub, refresh, MirrorConfig())

    async def scan(self) -> None:
        await plugin.SERVICE.scan_once()

    async def stop(self) -> None:
        await plugin.SERVICE.stop()
        await self.hub.close()
        self.registry.close()

    def entry(self) -> SessionEntry:
        return self.hub.entry(SESSION)


@pytest.fixture(autouse=True)
def by_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(self: AntigravityService, seconds: float, step: Any) -> None:
        return None

    monkeypatch.setattr(AntigravityService, "_every", never)


def control(entry: SessionEntry) -> str:
    return entry.session.control


def running(entry: SessionEntry) -> bool:
    """Whether the device runs a process for the session, read afresh."""
    return entry.runner is not None


async def refused(hub: SessionHub, text: str) -> RcError:
    with pytest.raises(RcError) as caught:
        await hub.send({"session_id": SESSION, "text": text})
    return caught.value


async def test_a_refused_send_grants_no_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path, monkeypatch)
    bench.peer.configure(list=bench.listed())
    await bench.start()
    try:
        await plugin.SERVICE.list_once()
        bench.terminal_in_the_project()
        first = await refused(bench.hub, "Go on.")
        assert (first.code, first.message) == ("conflict", errors.HELD)
        entry = bench.entry()
        assert entry.session.held, "A58 held it as the message reached it"
        assert not exemption.exempt(entry), "and that hold exempts it from nothing"
        assert control(entry) == "terminal"
        # The terminal stays in the project: every send is refused, as the first was.
        again = await refused(bench.hub, "Go on, please.")
        assert (again.code, again.message) == ("conflict", errors.HELD)
        # It quits, so the conversation is the device's again; another comes back.
        bench.terminal_gone()
        await bench.scan()
        assert control(entry) == "remote"
        bench.terminal_in_the_project()
        await bench.scan()
        assert control(entry) == "terminal", "rule 4 holds it again"
        third = await refused(bench.hub, "Now?")
        assert third.code == "conflict"
        assert "session/load" not in bench.peer.methods()
    finally:
        await bench.stop()


async def test_a_hand_off_the_device_runs_never_gets_the_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path, monkeypatch)
    bench.peer.configure(list=bench.listed())
    await bench.start()
    try:
        await plugin.SERVICE.list_once()
        result = await bench.hub.send({"session_id": SESSION, "text": "Go on."})
        assert result == {"accepted": "sent"}
        entry = bench.entry()
        await wait_for(lambda: entry.session.turn is None and entry.session.state == "idle")
        assert running(entry) and entry.session.held
        assert not exemption.exempt(entry), "continued here, but never created here"
        bench.terminal_in_the_project()
        await bench.scan()
        assert not running(entry), "its idle process gives way"
        assert control(entry) == "terminal"
        assert (await refused(bench.hub, "More.")).code == "conflict"
    finally:
        await bench.stop()


async def test_a_session_the_device_created_keeps_it_until_the_person_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bench = Bench(tmp_path, monkeypatch)
    await bench.start()
    try:
        await bench.hub.create(
            {"agent": "antigravity", "cwd": str(bench.work), "first_message": "Hello."}
        )
        entry = bench.entry()
        await wait_for(lambda: entry.session.turn is None and entry.session.state == "idle")
        assert exemption.exempt(entry)
        bench.terminal_in_the_project()
        await bench.scan()
        assert control(entry) == "remote", "a terminal in its directory leaves it alone"
        await bench.hub.archive({"session_id": SESSION, "archived": True})
        assert not exemption.exempt(entry)
        # Continued after the close, it is a hand-off like any other.
        bench.peer.configure(list=bench.listed())
        closed = await refused(bench.hub, "Back again.")
        assert (closed.code, closed.message) == ("conflict", errors.HELD)
        assert not exemption.exempt(entry), "the new hold does not bring the exemption back"
    finally:
        await bench.stop()


async def test_a_closed_session_found_by_the_scan_loses_the_exemption_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session closed with no process running is closed without its runner hearing of it."""
    bench = Bench(tmp_path, monkeypatch)
    await bench.start()
    try:
        await bench.hub.create({"agent": "antigravity", "cwd": str(bench.work)})
        entry = bench.entry()
        assert await bench.hub.release_process(entry) is True
        assert entry.runner is None and exemption.exempt(entry)
        await bench.hub.archive({"session_id": SESSION, "archived": True})
        assert bench.registry.get_kv(exemption.key(SESSION)) == "1", "no runner heard the close"
        await bench.scan()
        assert bench.registry.get_kv(exemption.key(SESSION)) == ""
        entry.session.held, entry.session.archived = True, False
        assert not exemption.exempt(entry)
    finally:
        await bench.stop()


async def test_a_hand_off_whose_turn_runs_is_given_up_when_the_turn_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 1 first: its own turn keeps it `remote` with one notice; then rule 4 holds it."""
    bench = Bench(tmp_path, monkeypatch, turns=["tool"])
    bench.peer.configure(list=bench.listed())
    await bench.start()
    try:
        await plugin.SERVICE.list_once()
        await bench.hub.send({"session_id": SESSION, "text": "List the files."})
        entry = bench.entry()

        def pending() -> list[dict[str, Any]]:
            return [
                f["event"]
                for f in bench.frames
                if f.get("type") == "session.event"
                and f["event"]["kind"] == "approval"
                and f["event"]["status"] == "pending"
            ]

        await wait_for(pending)
        bench.terminal_in_the_project()
        await bench.scan()
        await bench.scan()
        notices = [
            f["event"]["text"]
            for f in bench.frames
            if f.get("type") == "session.event" and f["event"]["kind"] == "notice"
        ]
        assert notices.count(errors.ALSO_OPEN) == 1
        assert control(entry) == "remote" and running(entry)
        assert entry.runner is not None
        await entry.runner.approve(pending()[0]["request_id"], "allow", None)
        await wait_for(lambda: not running(entry))
        assert control(entry) == "terminal", "given up as the turn ended"
        assert (await refused(bench.hub, "More.")).code == "conflict"
    finally:
        await bench.stop()
