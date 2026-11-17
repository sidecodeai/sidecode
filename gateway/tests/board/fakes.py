"""Stand-ins for the task board's surroundings: the hub, the index, the preferences and a model.

The board is driven here without a gateway: ``FakeHost`` answers ``session.history`` the way a
device does — the newest page first, ``before_seq`` paging back — and records every request and
every frame the board sends to an app. Time is a ``VirtualClock`` the test moves, so a debounce,
an interval or a back-off is crossed without waiting for it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rc_gateway.board.llm import Completion, Message
from rc_gateway.board.policy import BoardPolicy, OperatorPolicy
from rc_gateway.board.service import BoardService
from rc_gateway.board.store import BoardRow, BoardStore
from rc_gateway.connections import AppConnection
from rc_gateway.index import IndexedSession
from rc_gateway.preference_store import Preferences

from ..conftest import VirtualClock, fake_app

Event = dict[str, Any]
Frame = dict[str, Any]

DEVICE = "c5efb1ec-2912-4619-90f7-93b5172fd712"
SESSION = "ad2c9abb-4a1e-470a-835c-228778fc17f0"
OWNER = "ada"
#: A wall clock far from zero, so a millisecond timestamp looks like one.
START = 1_788_944_400.0
#: How long a test waits for the board to get somewhere: the store answers from a thread, and a
#: busy runner can be slow.
PATIENCE = 10.0


def block() -> str:
    return str(uuid.uuid4())


def person(seq: int, text: str, source: str = "remote", **extra: Any) -> Event:
    return {
        "seq": seq,
        "ts": seq,
        "kind": "user_message",
        "block_id": block(),
        "text": text,
        "source": source,
        **extra,
    }


def reply(seq: int, text: str, **extra: Any) -> Event:
    return {
        "seq": seq,
        "ts": seq,
        "kind": "assistant_text",
        "block_id": block(),
        "text": text,
        "done": True,
        **extra,
    }


def tool(seq: int, title: str, status: str = "succeeded", **extra: Any) -> Event:
    return {
        "seq": seq,
        "ts": seq,
        "kind": "tool_call",
        "block_id": block(),
        "tool": "Bash",
        "tool_kind": "shell",
        "title": title,
        "status": status,
        "started_at": seq,
        **extra,
    }


def rounds(count: int, *, start: int = 1, per_round: int = 3) -> list[Event]:
    """``count`` rounds of a message, a tool call and a reply, from seq ``start`` on."""
    events: list[Event] = []
    seq = start
    for number in range(1, count + 1):
        events.append(person(seq, f"request {number}"))
        for step in range(1, per_round - 1):
            events.append(tool(seq + step, f"step {number}.{step}"))
        events.append(reply(seq + per_round - 1, f"done with {number}"))
        seq += per_round
    return events


def answer(*items: tuple[str | None, str, str]) -> str:
    """A model's answer: (id, status, text) per item."""
    return json.dumps({"items": [{"id": i, "status": s, "text": t} for i, s, t in items]})


class FakeHost:
    """The hub as the board sees it, with one device's sessions behind it."""

    def __init__(self) -> None:
        #: Session → (device, account).
        self.owners: dict[str, tuple[str, str]] = {}
        self.online: set[str] = {DEVICE}
        self.history: dict[str, list[Event]] = {}
        self.queues: dict[str, Frame] = {}
        #: Every request the board sent a device, in order.
        self.asks: list[Frame] = []
        #: Every frame the board sent an app socket, with the socket.
        self.sent: list[tuple[AppConnection, Frame]] = []
        #: What the next history reads do instead of answering: "error", "silent" or "large".
        self.failing: str | None = None
        #: Set to hold every history read until it is set.
        self.gate: asyncio.Event | None = None
        self.reading = 0
        self.most_reading = 0

    def own(self, session_id: str = SESSION, username: str = OWNER, device: str = DEVICE) -> None:
        self.owners[session_id] = (device, username)

    async def ask_device(self, device_id: str, request: Frame, timeout: float) -> Frame | None:
        self.asks.append(dict(request))
        self.reading += 1
        self.most_reading = max(self.most_reading, self.reading)
        try:
            if self.gate is not None:
                await self.gate.wait()
            return self._page(device_id, request)
        finally:
            self.reading -= 1

    def _page(self, device_id: str, request: Frame) -> Frame | None:
        if self.failing == "silent" or device_id not in self.online:
            return None
        if self.failing == "error":
            return {"type": "reply", "ok": False, "error": {"code": "internal"}}
        events = sorted(self.history.get(request["session_id"], []), key=lambda e: e["seq"])
        before = request.get("before_seq")
        if before is not None:
            events = [event for event in events if event["seq"] < before]
        limit = request["limit"]
        page = events[-limit:]
        result: Frame = {"events": page, "has_more": len(events) > limit}
        if self.failing == "large":
            result["padding"] = "x" * (4 * 1024 * 1024 + 1)
        return {"type": "reply", "ok": True, "result": result}

    async def session_account(self, session_id: str) -> tuple[str, str] | None:
        return self.owners.get(session_id)

    async def send_app(self, connection: AppConnection, frame: Frame) -> None:
        self.sent.append((connection, frame))

    def queue_snapshot(self, session_id: str) -> Frame | None:
        return self.queues.get(session_id)

    def device_online(self, device_id: str) -> bool:
        return device_id in self.online

    def history_reads(self) -> list[Frame]:
        return [ask for ask in self.asks if ask.get("type") == "session.history"]

    def updates(self, connection: AppConnection | None = None) -> list[Frame]:
        return [
            frame
            for socket, frame in self.sent
            if frame.get("type") == "board.updated" and (connection is None or socket is connection)
        ]

    def replies(self, connection: AppConnection) -> list[Frame]:
        return [
            frame
            for socket, frame in self.sent
            if socket is connection and frame["type"] == "reply"
        ]


class FakeSessions:
    """The session index: how far each session has gone, and its state."""

    def __init__(self) -> None:
        self.rows: dict[str, IndexedSession] = {}

    def put(
        self,
        session_id: str = SESSION,
        *,
        state: str = "idle",
        last_seq: int = 0,
        archived: bool = False,
        device: str = DEVICE,
    ) -> IndexedSession:
        indexed = IndexedSession(
            session_id=session_id,
            device_id=device,
            state=state,
            last_seq=last_seq,
            marked=False,
            unseen=False,
            summary={"session_id": session_id, "state": state, "archived": archived},
        )
        self.rows[session_id] = indexed
        return indexed

    async def get(self, session_id: str) -> IndexedSession | None:
        return self.rows.get(session_id)


class FakePreferences:
    def __init__(self) -> None:
        self.values: dict[str, Preferences] = {}

    def turn(self, on: bool, username: str = OWNER) -> Preferences:
        self.values[username] = Preferences(task_board=on)
        return self.values[username]

    async def get(self, username: str) -> Preferences:
        return self.values.get(username, Preferences())


class FakeModel:
    """Answers each update from a script, ``{"items": []}`` once it runs out."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.script: list[str | Exception] = []
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.most_active = 0

    async def complete(self, messages: Sequence[Message]) -> Completion:
        self.calls.append(list(messages))
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            outcome = self.script.pop(0) if self.script else '{"items": []}'
            if isinstance(outcome, Exception):
                raise outcome
            return Completion(outcome, input_tokens=1200, output_tokens=40)
        finally:
            self.active -= 1

    def conversation(self, call: int = -1) -> str:
        return str(self.calls[call][1]["content"])


@dataclass
class Rig:
    service: BoardService
    host: FakeHost
    sessions: FakeSessions
    preferences: FakePreferences
    model: FakeModel
    store: BoardStore
    clock: VirtualClock
    sockets: list[AppConnection] = field(default_factory=list)

    def app(
        self, username: str = OWNER, *, subscribed: Sequence[str] = (SESSION,)
    ) -> AppConnection:
        connection = fake_app(username)
        for session_id in subscribed:
            connection.subscribe(session_id, 0)
        self.sockets.append(connection)
        return connection

    async def get(self, connection: AppConnection, session_id: str = SESSION) -> Frame:
        return await self._ask(connection, {"type": "board.get", "session_id": session_id})

    async def refresh(
        self, connection: AppConnection, session_id: str = SESSION, *, rebuild: bool = False
    ) -> Frame:
        frame = {"type": "board.refresh", "session_id": session_id, "rebuild": rebuild}
        return await self._ask(connection, frame)

    async def _ask(self, connection: AppConnection, frame: Frame) -> Frame:
        frame = {**frame, "id": str(uuid.uuid4())}
        await self.service.handle_app_frame(connection, frame)
        replies = [item for item in self.host.replies(connection) if item["id"] == frame["id"]]
        assert len(replies) == 1, replies
        return replies[0]

    def turn_ends(self, session_id: str = SESSION, *, last_seq: int) -> None:
        """A turn ran and ended: the index says so, as the hub tells the board."""
        previous = self.sessions.put(session_id, state="running", last_seq=last_seq)
        indexed = self.sessions.put(session_id, state="idle", last_seq=last_seq)
        self.service.on_session_change(previous, indexed, OWNER)

    def turn_starts(self, session_id: str = SESSION) -> None:
        previous = self.sessions.rows[session_id]
        indexed = self.sessions.put(session_id, state="running", last_seq=previous.last_seq)
        self.service.on_session_change(previous, indexed, OWNER)

    async def advance(self, seconds: float) -> None:
        await self.clock.advance(seconds)
        await settle()


async def make_rig(
    tmp_path: Path,
    *,
    board_on: bool = True,
    policy: Callable[[FakeModel], BoardPolicy] | None = None,
) -> Rig:
    clock = VirtualClock()
    clock.now = START
    host, sessions, preferences, model = FakeHost(), FakeSessions(), FakePreferences(), FakeModel()
    host.own()
    sessions.put()
    preferences.turn(board_on)
    store = BoardStore(tmp_path / "boards.sqlite3")
    service = BoardService(
        host,
        sessions,
        store,
        preferences,
        policy(model) if policy is not None else OperatorPolicy(model),
        clock=lambda: clock.now,
        sleep=clock.sleep,
    )
    return Rig(service, host, sessions, preferences, model, store, clock)


async def settle(seconds: float = 0.15) -> None:
    """Let the board's tasks run: the store answers from a thread, so give them real time."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)


async def until(check: Callable[[], bool], seconds: float = PATIENCE) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        assert asyncio.get_running_loop().time() < deadline, "the board never got there"
        await asyncio.sleep(0.005)


async def stored(
    rig: Rig, check: Callable[[BoardRow], bool], session_id: str = SESSION
) -> BoardRow:
    """The board's row, once it says what the test waits for."""
    deadline = asyncio.get_running_loop().time() + PATIENCE
    while True:
        row = await rig.store.get(session_id)
        if row is not None and check(row):
            return row
        assert asyncio.get_running_loop().time() < deadline, f"the board never got there: {row}"
        await asyncio.sleep(0.01)


async def told(rig: Rig, connection: AppConnection, state: str, *, after: int = 0) -> Frame:
    """The newest `board.updated` the socket got, once there are more than ``after`` of them
    and the newest says ``state``."""

    def arrived() -> bool:
        frames = rig.host.updates(connection)
        return len(frames) > after and frames[-1]["status"]["state"] == state

    await until(arrived)
    return rig.host.updates(connection)[-1]
