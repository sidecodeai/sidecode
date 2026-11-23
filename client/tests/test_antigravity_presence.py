"""The conservative presence rule for Antigravity conversations (A64, 4.4, A58).

The device never reads `agy`'s lock files, so it judges from the `agy` processes it did not start:
one naming the conversation (rule 1), one in its directory (rule 2), any at all for a conversation
without a directory (rule 3), and a session the device created and holds by rule 1 alone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from rc_client import procscan
from rc_client.agents.antigravity import errors, exemption, presence
from rc_client.agents.antigravity.holding import Holding
from rc_client.agents.antigravity.presence import Holder, Holders, Place
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry, SessionHub

# Taken before the suite's own stand-in replaces it for every test.
REAL_SCAN = presence.scan
SID = "d055434c-60ae-474a-b411-363f558a64c0"


def test_a_command_line_names_its_conversation_in_either_spelling() -> None:
    assert presence.conversation_of(["agy", "--conversation", SID]) == SID
    assert presence.conversation_of(["/x/agy", f"--conversation={SID}"]) == SID
    assert presence.conversation_of(["agy", "--dangerously-skip-permissions"]) is None
    assert presence.conversation_of(["agy", "--conversation"]) is None


def test_agy_is_known_by_its_program_not_the_name_it_was_started_under(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "antigravity-cli"
    binary.parent.mkdir()
    binary.write_text("")
    assert presence.is_agy("/Users/me/.local/bin/agy", None)
    assert presence.is_agy(str(binary), str(binary)), "the very file resolved, whatever its name"
    assert not presence.is_agy("/usr/bin/python3", str(binary))
    assert not presence.is_agy(None, str(binary))


@pytest.mark.parametrize(
    ("found", "place", "exempt", "held"),
    [
        # Rule 1, which even a session the device created and holds yields to.
        (Holders([Holder(1, SID)]), Place("/work"), True, True),
        # Rule 2: an agy in the conversation's directory, by real path.
        (Holders([Holder(1, None, "/work")]), Place("/work"), False, True),
        (Holders([Holder(1, None, "/elsewhere")]), Place("/work"), False, False),
        (Holders([Holder(1, None, "/work")]), Place("/work"), True, False),
        # Rule 3: no directory, and any agy at all.
        (Holders([Holder(1, None, "/elsewhere")]), Place("/home", True), False, True),
        (Holders([Holder(1, None, "/elsewhere")]), Place("/home", True), True, False),
        (Holders([]), Place("/home", True), False, False),
        # What a scan cannot say is not "nobody".
        (Holders(complete=False), Place("/work"), False, None),
        (Holders([Holder(1, None)], cwds=False), Place("/work"), False, None),
        (Holders([Holder(1, None)], cwds=False), Place("/home", True), False, True),
    ],
)
def test_the_rules(found: Holders, place: Place, exempt: bool, held: bool | None) -> None:
    assert presence.holds(SID, place, found, exempt=exempt) is held


def test_tmp_and_private_tmp_are_one_directory() -> None:
    if not Path("/private/tmp").is_dir():
        pytest.skip("only macOS has /private/tmp")
    found = Holders([Holder(1, None, "/tmp")])
    assert presence.holds(SID, Place("/private/tmp"), found, exempt=False) is True


async def test_the_scan_leaves_out_the_device_s_own_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    me = os.getpid()
    procs = [
        procscan.Proc(pid=101, ppid=1, start="s", command=f"agy --conversation {SID}"),
        procscan.Proc(pid=102, ppid=me, start="s", command="/Users/me/.local/bin/agy acp"),
        procscan.Proc(pid=103, ppid=102, start="s", command="agy -p /usage"),
        procscan.Proc(pid=104, ppid=1, start="s", command="node agy-wrapper"),
        procscan.Proc(pid=105, ppid=1, start="s", command="agy --dangerously-skip-permissions"),
        procscan.Proc(pid=106, ppid=1, start="s", command="vim agy"),
    ]

    async def table() -> procscan.Scan:
        return procscan.Scan(procs=procs, complete=True)

    async def programs(pids: list[int]) -> tuple[dict[int, str], bool]:
        paths = {
            101: "/Users/me/.local/bin/agy",
            102: "/Users/me/.local/bin/agy",
            103: "/Users/me/.local/bin/agy",
            104: "/Users/me/.local/bin/agy",
            105: "/Users/me/.local/bin/agy",
            106: "/usr/bin/vim",
        }
        return {pid: paths[pid] for pid in pids}, True

    async def cwds(pids: list[int]) -> tuple[dict[int, str], bool]:
        return {pid: f"/work/{pid}" for pid in pids}, True

    monkeypatch.setattr(procscan, "scan_processes", table)
    monkeypatch.setattr(presence, "_programs", programs)
    monkeypatch.setattr(procscan, "process_cwds", cwds)
    found = await REAL_SCAN(None, cwds=True)
    assert found.complete and found.cwds
    assert [(h.pid, h.conversation, h.cwd) for h in found.found] == [
        (101, SID, "/work/101"),
        (104, None, "/work/104"),
        (105, None, "/work/105"),
    ]


async def test_a_process_table_that_cannot_be_read_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def table() -> procscan.Scan:
        return procscan.Scan(procs=[], complete=False)

    monkeypatch.setattr(procscan, "scan_processes", table)
    assert (await REAL_SCAN(None, cwds=False)).complete is False


# ------------------------------------------------------------------ the hub


class Idle:
    """A device process for the session, with no turn of its own running unless told."""

    agent = "antigravity"
    private_process = True

    def __init__(self) -> None:
        self.closed = False
        self.busy = False

    async def close(self) -> None:
        self.closed = True


class Bench:
    def __init__(self, tmp_path: Path) -> None:
        self.registry = Registry(tmp_path / "state.sqlite3")
        self.frames: list[dict[str, Any]] = []

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        info = AgentInfo(agent="antigravity", available=True)
        self.hub = SessionHub(self.registry, publish, "dev-1", lambda: [info])
        self.holding = Holding(self.hub, lambda entry: Place(entry.session.cwd))

    def seed(self, **fields: Any) -> SessionEntry:
        session = Session(
            session_id=SID,
            device_id="dev-1",
            agent="antigravity",
            cwd=str(fields.pop("cwd", "/work")),
            state="idle",
            **fields,
        )
        entry = SessionEntry(
            session=session, channel=SessionChannel(self.registry, session, self.hub.publish)
        )
        self.hub.entries[SID] = entry
        return entry

    def notices(self) -> list[str]:
        return [
            f["event"]["text"]
            for f in self.frames
            if f.get("type") == "session.event" and f["event"]["kind"] == "notice"
        ]


def control(entry: SessionEntry) -> str:
    """The session's `control` now, read afresh."""
    return entry.session.control


@pytest.fixture
def bench(tmp_path: Path) -> Any:
    made = Bench(tmp_path)
    yield made
    made.registry.close()


async def test_an_idle_process_gives_way_and_the_session_is_held_by_the_terminal(
    bench: Bench,
) -> None:
    entry = bench.seed(origin="terminal", control="remote", held=True)
    runner = Idle()
    entry.runner = runner  # type: ignore[assignment]
    await bench.holding.apply(entry, True)
    assert runner.closed and entry.runner is None
    assert (entry.session.control, entry.session.state) == ("terminal", "readonly")
    # The holder quits: a session the device holds is the device's again (A58).
    await bench.holding.apply(entry, False)
    assert (entry.session.control, entry.session.state) == ("remote", "idle")


async def test_a_busy_process_warns_once_and_gives_way_when_its_turn_ends(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = bench.seed(origin="terminal", control="remote", held=True)
    runner = Idle()
    runner.busy = True
    entry.runner = runner  # type: ignore[assignment]
    await bench.holding.apply(entry, True)
    await bench.holding.apply(entry, True)
    assert bench.notices() == [errors.ALSO_OPEN], "said once"
    assert (entry.session.control, runner.closed) == ("remote", False)

    runner.busy = False

    async def holder(binary: str | None, *, cwds: bool) -> Holders:
        return Holders([Holder(7, SID)])

    monkeypatch.setattr(presence, "scan", holder)
    await bench.holding.settle(entry, None)
    assert runner.closed and control(entry) == "terminal"


async def test_a_conversation_nobody_holds_goes_back_to_the_archive(bench: Bench) -> None:
    entry = bench.seed(origin="terminal", control="none")
    await bench.holding.apply(entry, True)
    assert control(entry) == "terminal"
    await bench.holding.apply(entry, False)
    assert control(entry) == "none"


async def test_a_session_the_device_created_is_held_only_by_an_agy_naming_it(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = bench.seed(origin="remote", control="remote", held=True)
    exemption.mark(bench.registry, SID)
    found = Holders([Holder(9, None, "/work")])

    async def scan(binary: str | None, *, cwds: bool) -> Holders:
        return found

    monkeypatch.setattr(presence, "scan", scan)
    await bench.holding.scan(None)
    assert control(entry) == "remote", "a terminal in its directory leaves it alone"
    found = Holders([Holder(9, SID, "/elsewhere")])
    await bench.holding.scan(None)
    assert control(entry) == "terminal"


async def test_the_open_check_shows_the_hold_and_refuses(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = bench.seed(origin="terminal", control="none", cwd="/work")

    async def scan(binary: str | None, *, cwds: bool) -> Holders:
        return Holders([Holder(9, None, "/work")])

    monkeypatch.setattr(presence, "scan", scan)
    with pytest.raises(Exception) as caught:
        await bench.holding.check(entry, None)
    assert getattr(caught.value, "message", "") == errors.HELD
    assert entry.session.control == "terminal"
