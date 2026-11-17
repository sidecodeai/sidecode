"""The task board as the gateway runs it: the hooks the hub calls and the requests apps send (A62).

An app's `board.get` or `board.refresh` is answered at once from the stored board and a quick word
from the policy; whatever needs the device or the model happens in an update afterwards and reaches
the watching sockets as `board.updated`. While the account's task board is on, opening a board
follows it for `FOLLOW_DAYS`: a followed board is updated `TURN_END_DEBOUNCE_SECONDS` after each
turn of its session ends, whether or not an app watches it, and when its device says `hello` while
it is behind. A session nobody opened is never read.

The hub's hooks run on a device's read loop, so they only look at memory — the followed set, the
lanes, the watchers — and leave everything that touches the disk, the device or a model to a task.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from functools import partial

from ..connections import AppConnection
from ..frames import (
    BOARD_GET,
    BOARD_REFRESH,
    ERROR_BAD_REQUEST,
    ERROR_NOT_FOUND,
    error_reply,
    frame_type,
    ok_reply,
    request_id,
    text_field,
)
from ..index import IndexedSession
from ..logging import logger
from ..preference_store import Preferences
from .context import BoardHost, Context, PreferenceLookup, SessionLookup
from .follow import FOLLOW_MS, Follow
from .history import Frame
from .lanes import Lanes, Request
from .limits import DAY_SECONDS, TURN_END_DEBOUNCE_SECONDS, UNVIEWED_DAYS, VIEW_WRITE_SECONDS
from .policy import BoardPolicy
from .runner import Runner
from .store import BoardRow, BoardStore
from .watchers import Watchers
from .wire import (
    DEVICE_OFFLINE,
    STATUS_CURRENT,
    STATUS_OFF,
    STATUS_UPDATING,
    Status,
    board_view,
    stale,
    unavailable,
)

log = logger("rc_gateway.board")

#: A turn is under way in these states; leaving them for a resting one ends it.
ACTIVE_STATES = frozenset({"starting", "running", "needs_approval", "needs_input"})
RESTING_STATES = frozenset({"idle", "readonly", "stopped"})

OPEN = "open"
REFRESH = "refresh"
REBUILD = "rebuild"
TURN_END = "turn_end"
DEVICE_ONLINE = "device_online"
PREFERENCE = "preference"

UNVIEWED_MS = UNVIEWED_DAYS * DAY_SECONDS * 1000

Sleep = Callable[[float], Awaitable[None]]


def _archived(indexed: IndexedSession) -> bool:
    return indexed.summary.get("archived") is True


class BoardService:
    def __init__(
        self,
        host: BoardHost,
        sessions: SessionLookup,
        store: BoardStore,
        preferences: PreferenceLookup,
        policy: BoardPolicy,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._ctx = Context(
            host=host,
            sessions=sessions,
            store=store,
            preferences=preferences,
            policy=policy,
            clock=clock,
            watchers=Watchers(host.send_app),
        )
        self._runner = Runner(self._ctx, called=self._called)
        self._lanes = Lanes(self._runner.run, clock=clock, sleep=sleep)
        self._sleep = sleep
        self._tasks: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None

    def _called(self, session_id: str) -> None:
        self._lanes.called(session_id)

    # ---- lifecycle ----

    async def start(self) -> None:
        """Follow again the boards opened in the last `FOLLOW_DAYS`, and start the daily sweep."""
        now = self._ctx.now_ms()
        rows = await self._ctx.store.followed(now - FOLLOW_MS)
        self._ctx.followed.load((_follow(row) for row in rows), now)
        log.info("task boards ready", followed=len(self._ctx.followed))
        self._sweeper = asyncio.create_task(self._sweep_daily())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        await self._lanes.stop()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        closer = getattr(self._ctx.policy, "close", None)
        if closer is not None:
            await closer()

    async def _sweep_daily(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("board sweep failed")
            await self._sleep(DAY_SECONDS)

    async def sweep(self) -> None:
        """Delete the boards nobody opened for `UNVIEWED_DAYS` but those a socket watches, and
        forget lapsed follows."""
        ctx = self._ctx
        now = ctx.now_ms()
        removed = await ctx.store.sweep(now - UNVIEWED_MS, keep=ctx.watchers.watched_sessions())
        for session_id in removed:
            self._stop_following(session_id)
        ctx.followed.prune(now)
        self._lanes.prune()
        if removed:
            log.info("task boards swept", boards=len(removed))

    def _spawn(self, work: Callable[[], Awaitable[None]]) -> None:
        """Run ``work`` off the caller's path, as a task the shutdown ends."""
        task = asyncio.create_task(self._guarded(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guarded(work: Callable[[], Awaitable[None]]) -> None:
        try:
            await work()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("board task failed")

    # ---- app requests ----

    async def handle_app_frame(self, connection: AppConnection, frame: Frame) -> None:
        """`board.get` and `board.refresh`, answered from what is stored and a quick verdict."""
        kind = frame_type(frame)
        identifier = request_id(frame)
        if not identifier:
            log.debug("board request without an id dropped", kind=kind)
            return
        reply = await self._answer(connection, frame, kind, identifier)
        await self._ctx.host.send_app(connection, reply)

    async def _answer(
        self, connection: AppConnection, frame: Frame, kind: str, identifier: str
    ) -> Frame:
        session_id = text_field(frame, "session_id")
        rebuild = frame.get("rebuild", False) if kind == BOARD_REFRESH else False
        if not session_id:
            return error_reply(identifier, ERROR_BAD_REQUEST, "session_id is required")
        if not isinstance(rebuild, bool):
            return error_reply(identifier, ERROR_BAD_REQUEST, "rebuild must be a boolean")
        account = await self._ctx.host.session_account(session_id)
        # Another account's session is answered as one that does not exist (A24).
        if account is None or account[1] != connection.username:
            return error_reply(identifier, ERROR_NOT_FOUND, "unknown session")
        device_id, owner = account
        if kind == BOARD_GET:
            request = Request(OPEN)
        else:
            request = Request(REBUILD if rebuild else REFRESH, force=True, rebuild=rebuild)
        board, status = await self._engage(session_id, device_id, owner, request)
        # Watched before the reply leaves, and told what the reply says: an update that starts
        # afterwards reaches this socket too.
        self._ctx.watchers.watch(connection, session_id, device_id, board, status)
        if kind == BOARD_GET:
            return ok_reply(identifier, {"board": board, "status": status.view()})
        return ok_reply(identifier, {"status": status.view()})

    async def _engage(
        self, session_id: str, device_id: str, owner: str, request: Request
    ) -> tuple[Frame | None, Status]:
        """An app opened the board: record the view, and update it if it is behind."""
        ctx = self._ctx
        if not (await ctx.preferences.get(owner)).board_on:
            return None, STATUS_OFF
        now = ctx.now_ms()
        ctx.followed.view(Follow(session_id, device_id, owner, now))
        row = await self._viewed(session_id, device_id, owner, now)
        return board_view(row), await self._decide(row, request)

    async def _viewed(self, session_id: str, device_id: str, owner: str, now: int) -> BoardRow:
        """The board's row, created empty the first time, its view written once a while."""
        store = self._ctx.store
        row = await store.get(session_id)
        if row is not None and row.username != owner:
            await store.remove(session_id)
            row = None
        if row is None:
            await store.create(session_id, device_id, owner, now)
            return BoardRow(session_id, device_id, owner, viewed_at=now)
        if now - row.viewed_at >= VIEW_WRITE_SECONDS * 1000:
            await store.viewed(session_id, now)
        return row

    async def _decide(self, row: BoardRow, request: Request) -> Status:
        """The board's status for a reply, starting an update when one is due."""
        ctx = self._ctx
        refusal = await ctx.refusal(row.username)
        if refusal is not None:
            return unavailable(refusal)
        if request.rebuild:
            self._lanes.request(row.session_id, request)
            return STATUS_UPDATING
        if self._lanes.busy(row.session_id):
            if request.force:
                self._lanes.request(row.session_id, request)
            return STATUS_UPDATING
        if not await ctx.behind(row.session_id, row):
            return STATUS_CURRENT
        held = ctx.held_back(row, force=request.force)
        if held is not None:
            return held
        if not ctx.host.device_online(row.device_id):
            return stale(DEVICE_OFFLINE)
        self._lanes.request(row.session_id, request)
        return STATUS_UPDATING

    # ---- hub hooks: memory only ----

    def on_session_change(
        self, previous: IndexedSession | None, indexed: IndexedSession, owner: str
    ) -> None:
        """A followed session's turn ended: update its board once the debounce has passed."""
        if previous is None:
            return
        session_id = indexed.session_id
        if self._ctx.followed.get(session_id, self._ctx.now_ms()) is None:
            return
        if indexed.state in ACTIVE_STATES:
            self._lanes.cancel_debounce(session_id)
            return
        ended = previous.state in ACTIVE_STATES and indexed.state in RESTING_STATES
        if not ended or _archived(indexed):
            return
        self._lanes.debounce(session_id, TURN_END_DEBOUNCE_SECONDS, Request(TURN_END))
        if self._ctx.watchers.watched(session_id):
            self._spawn(partial(self._ctx.watchers.publish_status, session_id, STATUS_UPDATING))

    def on_device_online(self, device_id: str) -> None:
        """A device said `hello`: its followed boards that fell behind are updated, in turn."""
        follows = self._ctx.followed.of_device(device_id, self._ctx.now_ms())
        if follows:
            self._spawn(partial(self._catch_up, follows))

    def on_session_dropped(self, session_id: str) -> None:
        """A session left the gateway: its board goes with it."""
        self._forget_session(session_id)
        self._spawn(partial(self._ctx.store.remove, session_id))

    def on_app_detached(self, connection: AppConnection) -> None:
        self._ctx.watchers.forget_socket(connection)

    async def _catch_up(self, follows: list[Follow]) -> None:
        for follow in follows:
            await self._nudge(follow, Request(DEVICE_ONLINE))

    async def _nudge(self, follow: Follow, request: Request) -> None:
        """Start an update of a followed board that is behind and may be updated now."""
        ctx = self._ctx
        row = await ctx.store.get(follow.session_id)
        if row is None or row.username != follow.username or self._lanes.busy(row.session_id):
            return
        if await ctx.refusal(follow.username) is not None:
            return
        if not await ctx.behind(row.session_id, row) or ctx.held_back(row, force=False):
            return
        self._lanes.request(row.session_id, request)

    # ---- the account's preference, and deletion ----

    async def on_preferences(self, username: str, preferences: Preferences) -> None:
        """The account's task board was turned on or off, or another preference changed."""
        watchers = self._ctx.watchers
        if preferences.board_on:
            for session_id in watchers.sessions_of(username):
                account = await self._ctx.host.session_account(session_id)
                if account is None or account[1] != username:
                    continue
                request = Request(PREFERENCE)
                board, status = await self._engage(session_id, account[0], username, request)
                await watchers.publish_board(session_id, board, status)
            return
        watched = watchers.sessions_of(username)
        removed = await self._forget_user(username)
        if removed or watched:
            log.info("task boards turned off", username=username, followed=removed)
        for session_id in watched:
            await watchers.publish_board(session_id, None, STATUS_OFF)

    async def forget_account(self, username: str) -> None:
        """A deleted account takes every board and follow with it."""
        await self._forget_user(username)

    async def forget_device(self, device_id: str) -> None:
        """A revoked device takes the boards of its sessions with it, any left over included."""
        for follow in self._ctx.followed.of_device(device_id, self._ctx.now_ms()):
            self._forget_session(follow.session_id)
        self._ctx.followed.drop_device(device_id)
        self._runner.forget_device(device_id)
        await self._ctx.store.remove_for_device(device_id)

    async def _forget_user(self, username: str) -> int:
        """Every board and follow of the account. Its sockets keep watching: they are told."""
        dropped = self._ctx.followed.drop_user(username)
        for session_id in dropped:
            self._stop_following(session_id)
        self._ctx.stop_user_runs(username)
        self._ctx.verdicts.clear(username)
        await self._ctx.store.remove_for_user(username)
        return len(dropped)

    def _forget_session(self, session_id: str) -> None:
        self._stop_following(session_id)
        self._ctx.watchers.forget_session(session_id)

    def _stop_following(self, session_id: str) -> None:
        """No more updates of the session's board, and none still running writes or sends."""
        self._ctx.followed.drop(session_id)
        self._ctx.stop_runs(session_id)
        self._lanes.drop(session_id)
        self._ctx.failures.pop(session_id, None)


def _follow(row: BoardRow) -> Follow:
    return Follow(row.session_id, row.device_id, row.username, row.viewed_at)
