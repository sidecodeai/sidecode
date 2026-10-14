"""The routing core: one device slot per device, request forwarding, replay fan-out.

Adapted in structure from cc-remote's ``cc_remote/relay/pairing.py`` (MIT, see
THIRD_PARTY_NOTICES.md). What is kept: one connection slot per device with replacement, a send lock
that linearises forwarding against slot changes, and the "drop the whole slow connection" rule.
What is new here: request/reply correlation with gateway-generated ``device_offline`` and
``timeout`` replies, the per-session replay buffer with per-connection cursors, and the session
index.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from .auto_update import (
    BUSY_STATES,
    UPDATE_RETRY_SECONDS,
    AutoUpdate,
    DeviceUpdates,
)
from .badge import BadgeChange, BadgeHook
from .budget import ByteBudget
from .connections import AppConnection, DeviceConnection, SlowClientError, encode
from .devices import UPDATE_FAILED, UPDATE_RUNNING, DeviceStore, normalize_pairing_code
from .frames import (
    CLOSE_DEVICE_REPLACED,
    CLOSE_FORBIDDEN,
    CLOSE_SLOW_CLIENT,
    CLOSE_TOO_MANY_APPS,
    CLOSE_UNAUTHORIZED,
    DEVICE_PUSH_TYPES,
    DEVICE_UPDATE,
    DEVICE_UPDATE_FAILED,
    ERROR_BAD_REQUEST,
    ERROR_DEVICE_OFFLINE,
    ERROR_NOT_FOUND,
    ERROR_TIMEOUT,
    ERROR_TOO_LARGE,
    FORWARDED_BY_DEVICE,
    FORWARDED_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_EVENT_BYTES,
    PING_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    REQUEST_TIMEOUT_SECONDS,
    SESSION_RESULT_TYPES,
    SILENT_TIMEOUT_SECONDS,
    TERMINAL_ATTACH,
    TERMINAL_CLOSE,
    TERMINAL_DETACH,
    TERMINAL_EXITED,
    TERMINAL_OPEN,
    TERMINAL_PUSH_TYPES,
    Frame,
    bool_field,
    error_reply,
    frame_type,
    int_field,
    object_field,
    ok_reply,
    request_id,
    text_field,
)
from .index import IndexedSession, SessionIndex
from .logging import logger
from .replay import ReplayBuffer
from .terminals import TerminalRoutes
from .unseen import next_unseen, with_mark
from .users import UserRecord
from .views import device_view, has_available_agent, user_view

log = logger("rc_gateway.hub")

DEVICE_REPLACED_CLOSE_REASON = "replaced by a newer connection"
HEARTBEAT_TICK_SECONDS = 5.0
MAX_TRACKED_PAIRINGS = 64
#: Pairing progress is tracked only as long as the code can still be redeemed. Without this a
#: device with no agent installed never reaches the `agents` step and its entry would linger,
#: absorbing the `online` step of a later re-enrollment.
PAIRING_TRACK_SECONDS = 600.0
#: Amendment A9. A gateway-originated request carries this opaque id in `from`, so the device
#: echoes it like any other and the reply routes back here instead of to an app connection.
GATEWAY_ORIGIN_ID = "gateway"
BACKFILL_LIMIT = 1000
#: A device that was offline for a long time can be far ahead; stop after this many pages rather
#: than looping forever against a peer that keeps reporting `has_more`.
MAX_BACKFILL_PAGES = 64
ACTIVE_APP_WINDOW_SECONDS = 60.0
#: How long a shutdown waits for notifications that are already on their way out.
TRANSITION_DRAIN_SECONDS = 5.0
#: Amendment A13. A dropped device link is not an offline device: mobile NAT, a suspended laptop
#: and a daemon restart all look identical for a few seconds, and reporting each one as offline
#: made devices flap. `online` stays true for this long, and a reconnect inside it is invisible.
OFFLINE_GRACE_SECONDS = 20.0
#: Close codes that mean the device is not coming back, so `online` flips at once (A13, §2.5).
IMMEDIATE_OFFLINE_CLOSES = frozenset({CLOSE_UNAUTHORIZED, CLOSE_FORBIDDEN})

#: Replay buffers hold up to 4 MiB each (§6, a wire rule the gateway may not change), so the map
#: itself has to be bounded by a number whose product fits the container. 16 buffers of 4 MiB is
#: 64 MiB, which is the share `budget.py`'s arithmetic leaves for them inside `mem_limit: 512m`;
#: the old 512 stated a 2 GiB ceiling, four times the whole container. Buffers rarely fill, and
#: evicting the coldest only costs the next subscriber a `resync: true`, which the protocol
#: already handles by paging history from the device.
MAX_REPLAY_BUFFERS = 16

#: The newest `queue` snapshot per session (A6) and the device that owns each session are both
#: keyed by a session id a device chose, and entries leave only when a session is deleted or its
#: device revoked: without a cap a long-lived device grows either map without bound. A snapshot is
#: at most `MAX_EVENT_BYTES`, so 512 of them is 32 MiB; an owner is one device id, so 8192 of them
#: is under a megabyte. Evicting either costs nothing but a read: `_owner_of` falls back to the
#: index, and a session with no snapshot simply has none to replay.
MAX_TRACKED_QUEUES = 512
MAX_TRACKED_SESSION_OWNERS = 8192

#: What a device may announce in `hello` and `agents.updated`, matching what
#: `POST /api/devices/enroll` already truncates to. The per-entry ceiling is §4's event ceiling:
#: an agent description is a handful of models and modes, never larger than one event.
MAX_ANNOUNCED_AGENTS = 16
MAX_AGENT_ENTRY_BYTES = MAX_EVENT_BYTES

#: A31 aside: an account's app sockets. The web app, a phone and a Mac need three or four, so eight
#: is generous; a ninth closes the oldest rather than letting one account hold sockets until the
#: container runs out of them. Every broadcast is linear in this number.
MAX_APPS_PER_USER = 8
APP_REPLACED_CLOSE_REASON = "too many app connections for this account"

#: The gateway is holding as much of a large payload as it can, and this one has to wait. §1 has
#: no "busy", and `too_large` is the code for a payload over a documented bound — this is that
#: bound, stated in `budget.py`.
FORWARD_BUSY_MESSAGE = "the gateway is forwarding another large message; try again"

#: Session states that mean a turn is in progress (``needs_*`` are sub-states of running, §3).
ACTIVE_STATES = frozenset({"running", "needs_approval", "needs_input"})

#: Device owners the hub has resolved (A24). Bounded because it is keyed by device, and an account
#: may hold at most `MAX_ACTIVE_DEVICES`; a deleted device's entry is dropped with its sessions.
MAX_TRACKED_OWNERS = 4096

#: A22. A device that accepted an update installs a wheel and restarts its service, so it is gone
#: for a few seconds. Past this it is not coming back on its own and the row says so.
UPDATE_TIMEOUT_SECONDS = 300.0
UPDATE_TIMEOUT_MESSAGE = "the device did not come back"


@dataclass(frozen=True)
class SessionTransition:
    """One session's state change, as the push trigger receives it."""

    previous_state: str
    state: str
    session: Frame
    #: The account that owns the device: only its registrations are notified (A24).
    owner: str
    has_active_subscriber: bool


TransitionHook = Callable[[SessionTransition], Awaitable[None]]


@dataclass(frozen=True)
class SessionResumeNotice:
    """A ``resume`` event a device published, as the push trigger receives it (A35, §5.15).

    This one is not a state change: the session stays idle while a resume waits, so the device's
    decision is the only thing that says a person should be told. Which statuses are worth a
    notification is push's to decide; the hub reports every one of them.
    """

    status: str
    session_id: str
    device_id: str
    #: The account that owns the device: only its registrations are notified (A24).
    owner: str
    has_active_subscriber: bool


ResumeHook = Callable[[SessionResumeNotice], Awaitable[None]]


@dataclass
class _Pending:
    device_id: str
    connection_id: str
    #: The frame type that was forwarded; ``device.update`` moves the device on an accepted reply.
    kind: str = ""
    #: A38: the terminal a ``terminal.*`` request named, so an accepted ``terminal.close`` is
    #: forgotten by the id the request carried rather than by one the reply does not repeat.
    terminal_id: str = ""
    timer: asyncio.Task[None] | None = None


@dataclass
class _GatewayRequest:
    """A request the gateway made on its own account (A9), waiting for the device's reply.

    The frame type travels with it so the reply is acted on where every other reply is — on the
    device's read loop, in frame order — rather than in whatever task happens to be waiting.
    """

    kind: str
    waiter: asyncio.Future[Frame]


@dataclass
class _Grace:
    """A device whose link dropped, still reported online while it might come back (A13)."""

    #: Set when the grace resolves, whichever way: a replacement connection arrived, or the
    #: period ran out. A request parked on it re-reads the device slot and learns which.
    resolved: asyncio.Event
    timer: asyncio.Task[None] | None = None


@dataclass
class _Pairing:
    code: str
    created_at: float
    #: The account that minted the code. Its progress is nobody else's business (A24).
    username: str = ""
    device_id: str = ""
    steps: set[str] = field(default_factory=set)


class Hub:
    def __init__(
        self,
        index: SessionIndex,
        device_store: DeviceStore,
        *,
        on_session_transition: TransitionHook | None = None,
        on_session_resume: ResumeHook | None = None,
        on_badge_change: BadgeHook | None = None,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        offline_grace: float = OFFLINE_GRACE_SECONDS,
        update_timeout: float = UPDATE_TIMEOUT_SECONDS,
        update_retry: float = UPDATE_RETRY_SECONDS,
        served_build: Callable[[], str | None] | None = None,
    ) -> None:
        self.index = index
        self.device_store = device_store
        self.request_timeout = request_timeout
        self.offline_grace = offline_grace
        self.update_timeout = update_timeout
        self._on_session_transition = on_session_transition
        self._on_session_resume = on_session_resume
        self._on_badge_change = on_badge_change
        self._devices: dict[str, DeviceConnection] = {}
        self._apps: dict[str, AppConnection] = {}
        self._buffers: dict[str, ReplayBuffer] = {}
        self._owners: dict[str, str] = {}
        #: device id → the account that enrolled it, so a frame is published to that account only.
        self._device_owners: dict[str, str] = {}
        self._queues: dict[str, Frame] = {}
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._gateway_pending: dict[str, _GatewayRequest] = {}
        self._backfills: set[asyncio.Task[None]] = set()
        self._notifications: set[asyncio.Task[None]] = set()
        self._grace: dict[str, _Grace] = {}
        self._updates: dict[str, asyncio.Task[None]] = {}
        self._pairings: dict[str, _Pairing] = {}
        #: A38: which app connection each open terminal streams to.
        self._terminals = TerminalRoutes()
        self._budget = ByteBudget()
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._heartbeat: asyncio.Task[None] | None = None
        # A36. A gateway with no wheel to serve has nothing to bring a device to, so a hub built
        # without one simply never asks.
        self._auto_update = AutoUpdate(
            DeviceUpdates(
                ask=self._ask_device,
                connection=self._device_connection,
                record=self.device_store.get,
                busy=self._device_is_busy,
            ),
            served_build=served_build if served_build is not None else lambda: None,
            retry_after=update_retry,
        )

    # ---- lifecycle ----

    async def start(self) -> None:
        if self._heartbeat is None:
            self._heartbeat = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat
            self._heartbeat = None
        for task in list(self._backfills):
            task.cancel()
        self._backfills.clear()
        for device_id in list(self._grace):
            self._end_grace(device_id)
        for device_id in list(self._updates):
            self._cancel_update_timer(device_id)
        self._auto_update.stop()
        await self._drain_notifications()
        for waiting in self._gateway_pending.values():
            waiting.waiter.cancel()
        self._gateway_pending.clear()
        for pending in list(self._pending.values()):
            if pending.timer is not None:
                pending.timer.cancel()
        self._pending.clear()
        for connection in [*self._devices.values(), *self._apps.values()]:
            await connection.stop(code=1001, reason="gateway shutting down")
        self._devices.clear()
        self._apps.clear()

    async def _drain_notifications(self) -> None:
        """Let the pushes already in flight finish, rather than dropping a notification.

        A vendor that has stopped answering must not hold the shutdown open, so whatever is still
        running when the grace period ends is cancelled.
        """
        pending = list(self._notifications)
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=TRANSITION_DRAIN_SECONDS)
            for task in unfinished:
                task.cancel()
        self._notifications.clear()

    def device_online(self, device_id: str) -> bool:
        """True while the device holds its slot, and through the A13 grace period after it drops."""
        return device_id in self._devices or device_id in self._grace

    def online_device_ids(self) -> list[str]:
        return sorted(self._devices)

    def latency_for(self, device_id: str) -> int | None:
        connection = self._devices.get(device_id)
        return connection.latency_ms if connection is not None else None

    def agents_for(self, device_id: str) -> list[dict[str, Any]]:
        connection = self._devices.get(device_id)
        return list(connection.agents) if connection is not None else []

    @property
    def app_count(self) -> int:
        return len(self._apps)

    # ---- device side ----

    async def attach_device(self, connection: DeviceConnection) -> None:
        """Claim the device's single slot, closing any previous connection."""
        await self.device_owner(connection.device_id)
        async with self._send_lock, self._lock:
            previous = self._devices.get(connection.device_id)
            self._devices[connection.device_id] = connection
        # A13: a replacement inside the grace period ends it without a word to the apps, which
        # never saw the device leave, and releases whatever requests were parked waiting for it.
        reconnected = self._end_grace(connection.device_id)
        if previous is not None and previous is not connection:
            await previous.stop(code=CLOSE_DEVICE_REPLACED, reason=DEVICE_REPLACED_CLOSE_REASON)
        log.info("device connected", device_id=connection.device_id, within_grace=reconnected)

    async def detach_device(self, connection: DeviceConnection) -> None:
        # A36: an attempt belongs to the connection that earned it; the next hello starts over.
        self._auto_update.forget(connection)
        async with self._lock:
            if self._devices.get(connection.device_id) is not connection:
                return
            del self._devices[connection.device_id]
        # A38: the shells were the daemon's children. Whatever survives a mere link drop will be
        # detached again by the first output frame addressed to a connection that cannot take it.
        self._terminals.release_device(connection.device_id)
        code = connection.close_code
        log.info("device disconnected", device_id=connection.device_id, close_code=code)
        if code in IMMEDIATE_OFFLINE_CLOSES:
            await self._report_offline(connection.device_id)
            return
        self._begin_grace(connection.device_id)

    def _begin_grace(self, device_id: str) -> None:
        """Hold a dropped device online for the grace period (A13)."""
        self._end_grace(device_id)
        grace = _Grace(resolved=asyncio.Event())
        grace.timer = asyncio.create_task(self._expire_grace(device_id, grace))
        self._grace[device_id] = grace

    def _end_grace(self, device_id: str) -> bool:
        """Drop a device's grace without reporting anything. True when one was running."""
        grace = self._grace.pop(device_id, None)
        if grace is None:
            return False
        if grace.timer is not None:
            grace.timer.cancel()
        grace.resolved.set()
        return True

    async def _expire_grace(self, device_id: str, grace: _Grace) -> None:
        try:
            await asyncio.sleep(self.offline_grace)
        except asyncio.CancelledError:
            return
        if self._grace.get(device_id) is not grace:
            return
        del self._grace[device_id]
        # Released before the broadcast, so a parked request is answered without also waiting for
        # every app socket to take the frame.
        grace.resolved.set()
        log.info("device offline: no reconnect within the grace period", device_id=device_id)
        await self._report_offline(device_id)

    async def _report_offline(self, device_id: str) -> None:
        await self._fail_pending_for_device(device_id, ERROR_DEVICE_OFFLINE)
        record = await self.device_store.get(device_id)
        if record is not None:
            await self.broadcast_user(
                record.username,
                {
                    "type": "device.updated",
                    "device": device_view(record, online=False, latency_ms=None),
                },
            )

    async def _await_device(self, device_id: str) -> None:
        """Hold a request while its device is inside the grace period (A13, §2.5).

        A link that dropped is not an offline device, so a message sent in the seconds around a
        reconnect waits for the replacement connection rather than being refused. The wait is
        bounded by the period itself: when it ends without one, the caller re-reads the slot,
        finds it empty and answers ``device_offline`` as before.
        """
        if device_id in self._devices:
            return
        grace = self._grace.get(device_id)
        if grace is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(grace.resolved.wait(), timeout=self.offline_grace)

    async def disconnect_device(
        self, device_id: str, *, reason: str, code: int = CLOSE_UNAUTHORIZED
    ) -> bool:
        """Close a device's socket because its credential is gone (A4: 4401, do not retry).

        A disabled account is the other case: the token is still genuine and the device must not
        treat the close as a reason to re-enrol, so that one closes with 4403 (A24).
        """
        # An explicit removal is not a transient drop: end any grace so nothing later reports
        # this device as merely offline, and release requests parked on it (A13).
        self._end_grace(device_id)
        async with self._lock:
            connection = self._devices.get(device_id)
        if connection is None:
            return False
        await connection.stop(code=code, reason=reason)
        return True

    async def handle_device_hello(self, connection: DeviceConnection, frame: Frame) -> bool:
        """Apply a device hello. Returns False when the frame is unusable."""
        if int_field(frame, "protocol") != PROTOCOL_VERSION:
            return False
        connection.hello = frame
        connection.agents = _agent_list(frame.get("agents"))
        await self.device_store.describe(
            connection.device_id,
            agents=connection.agents,
            name=text_field(frame, "name"),
            platform=text_field(frame, "platform"),
            hostname=text_field(frame, "hostname"),
            arch=text_field(frame, "arch"),
            client_version=text_field(frame, "client_version"),
            # A38. Absent means a client older than the amendment, which is not the same as a
            # client that offers no shell, so the `Device` keeps the field out rather than false.
            terminal=bool_field(frame, "terminal"),
        )
        # A22: the device that comes back is the outcome of any update it was asked for, so the
        # build it announces lands before the apps are told anything about it.
        self._cancel_update_timer(connection.device_id)
        client_build = text_field(frame, "client_build") or None
        await self.device_store.record_build(connection.device_id, client_build)
        await self.device_store.touch(connection.device_id)
        sessions = frame.get("sessions")
        if isinstance(sessions, list):
            for summary in sessions:
                if isinstance(summary, dict):
                    await self._store_session(connection.device_id, summary)
        await self._announce_device(connection)
        # A36: a device behind the served wheel is brought to it without anyone pressing Update.
        self._auto_update.on_hello(connection, client_build)
        self._start_backfill(connection, sessions if isinstance(sessions, list) else [])
        await self._advance_pairing(connection.device_id, "online")
        if has_available_agent(connection.agents):
            await self._advance_pairing(connection.device_id, "agents")
        return True

    # ---- backfill after a link outage (A9) ----

    def _start_backfill(self, connection: DeviceConnection, summaries: list[Any]) -> None:
        """Ask the device for whatever the replay buffer missed while the link was down.

        Only a session whose buffer still holds something is worth backfilling: an empty buffer
        already answers `session.subscribe` with `resync`, and the app pages history itself.
        """
        plan: list[tuple[str, int]] = []
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            session_id = text_field(summary, "session_id")
            device_last = int_field(summary, "last_seq")
            buffer = self._buffers.get(session_id)
            if not session_id or device_last is None or buffer is None or len(buffer) == 0:
                continue
            if device_last > buffer.last_seq:
                plan.append((session_id, buffer.last_seq))
        if not plan:
            return
        task = asyncio.create_task(self._run_backfill(connection, plan))
        self._backfills.add(task)
        task.add_done_callback(self._backfills.discard)

    async def _run_backfill(
        self, connection: DeviceConnection, plan: list[tuple[str, int]]
    ) -> None:
        for session_id, tail in plan:
            try:
                await self._backfill_session(connection, session_id, tail)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("backfill failed", device_id=connection.device_id)

    async def _backfill_session(
        self, connection: DeviceConnection, session_id: str, tail: int
    ) -> None:
        after = tail
        for _ in range(MAX_BACKFILL_PAGES):
            reply = await self._ask_device(
                connection,
                {
                    "type": "session.history",
                    "session_id": session_id,
                    "after_seq": after,
                    "limit": BACKFILL_LIMIT,
                },
            )
            if reply is None or not reply.get("ok"):
                return
            result = object_field(reply, "result") or {}
            events = result.get("events")
            if not isinstance(events, list) or not events:
                return
            applied = 0
            for event in events:
                if not isinstance(event, dict):
                    continue
                seq = int_field(event, "seq")
                if seq is None or seq <= after:
                    continue
                frame = {"type": "session.event", "session_id": session_id, "event": event}
                if await self._publish_event(connection, session_id, event, frame):
                    applied += 1
                    after = max(after, seq)
            log.info(
                "backfilled events after a device reconnect",
                device_id=connection.device_id,
                events=applied,
                through_seq=after,
            )
            if not result.get("has_more") or applied == 0:
                return

    async def _ask_device(self, connection: DeviceConnection, request: Frame) -> Frame | None:
        """Send a gateway-originated request and wait for the device's reply."""
        kind = frame_type(request)
        # A UUID, not a bare hex string: every request id on the wire is one (§6.3 `RequestBase`),
        # and a device validating what it receives would refuse the gateway's own requests.
        identifier = str(uuid.uuid4())
        waiter: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
        self._gateway_pending[identifier] = _GatewayRequest(kind=kind, waiter=waiter)
        frame = {
            **request,
            "id": identifier,
            "from": GATEWAY_ORIGIN_ID,
            "device_id": connection.device_id,
        }
        try:
            async with self._send_lock:
                await connection.send(frame)
            return await asyncio.wait_for(waiter, timeout=self.request_timeout)
        except (SlowClientError, ConnectionError):
            log.warning(
                "gateway request could not be sent", device_id=connection.device_id, kind=kind
            )
            return None
        except (TimeoutError, asyncio.CancelledError):
            log.warning("gateway request timed out", device_id=connection.device_id, kind=kind)
            return None
        finally:
            self._gateway_pending.pop(identifier, None)

    async def handle_device_frame(self, connection: DeviceConnection, frame: Frame) -> None:
        kind = frame_type(frame)
        if kind == "pong":
            connection.note_pong()
            return
        if kind == "reply":
            await self._route_reply(connection, frame)
            return
        if kind not in DEVICE_PUSH_TYPES:
            log.debug("unknown device frame dropped", device_id=connection.device_id, kind=kind)
            return
        if kind in TERMINAL_PUSH_TYPES:
            await self._route_terminal(connection, kind, frame)
        elif kind == "session.event":
            await self._handle_session_event(connection, frame)
        elif kind == "session.updated":
            summary = object_field(frame, "session")
            if summary is not None:
                await self._store_session(connection.device_id, summary)
        elif kind == "session.removed":
            session_id = text_field(frame, "session_id")
            if session_id:
                await self._forget_session(connection.device_id, session_id)
        elif kind == "agents.updated":
            connection.agents = _agent_list(frame.get("agents"))
            await self.device_store.describe(connection.device_id, agents=connection.agents)
            await self._announce_device(connection)
            if has_available_agent(connection.agents):
                await self._advance_pairing(connection.device_id, "agents")
        elif kind == DEVICE_UPDATE_FAILED:
            await self._fail_update(
                connection.device_id, text_field(frame, "message") or "the update did not complete"
            )

    # ---- client updates (A22, A36) ----

    async def _begin_update(self, device_id: str) -> None:
        """The device accepted ``device.update``: it is away until it returns, or until it fails.

        Whoever asked — the gateway itself or an app's Retry — an update that starts clears the
        build remembered from the last one that failed, so a wheel is worth trying again (A36).
        """
        await self.device_store.set_update(device_id, UPDATE_RUNNING)
        await self._announce_stored_device(device_id)
        self._arm_update_timer(device_id)

    async def _fail_update(self, device_id: str, message: str) -> None:
        self._cancel_update_timer(device_id)
        await self.device_store.set_update(
            device_id, UPDATE_FAILED, message, failed_build=self._auto_update.served_build()
        )
        await self._announce_stored_device(device_id)

    def _arm_update_timer(self, device_id: str) -> None:
        self._cancel_update_timer(device_id)
        self._updates[device_id] = asyncio.create_task(self._expire_update(device_id))

    def _cancel_update_timer(self, device_id: str) -> None:
        timer = self._updates.pop(device_id, None)
        if timer is not None:
            timer.cancel()

    async def _expire_update(self, device_id: str) -> None:
        try:
            await asyncio.sleep(self.update_timeout)
        except asyncio.CancelledError:
            return
        self._updates.pop(device_id, None)
        record = await self.device_store.get(device_id)
        if record is None or record.update_state != UPDATE_RUNNING:
            return
        log.warning("client update did not complete", device_id=device_id)
        await self.device_store.set_update(
            device_id,
            UPDATE_FAILED,
            UPDATE_TIMEOUT_MESSAGE,
            failed_build=self._auto_update.served_build(),
        )
        await self._announce_stored_device(device_id)

    def _device_connection(self, device_id: str) -> DeviceConnection | None:
        return self._devices.get(device_id)

    async def _device_is_busy(self, device_id: str) -> bool:
        """Whether a session on this device is mid-turn, counted as the device counts them (A22).

        Read only while an automatic update is parked waiting for the machine to go quiet, so the
        index lookup is nowhere near the path a session event takes.
        """
        sessions = await self.index.list_sessions(device_id=device_id)
        return any(text_field(item, "state") in BUSY_STATES for item in sessions)

    # ---- terminals (A38, §7.3) ----

    async def _route_terminal(self, device: DeviceConnection, kind: str, frame: Frame) -> None:
        """Hand one terminal frame to the connection that holds it, and to nobody else.

        A terminal is one person's view: the frame goes to the socket ``to`` names, with ``to``
        stripped and the device identity the token proved put in its place, and only when that
        socket signed in as the account that owns the machine. Nothing here reads ``data``; the
        gateway relays bytes and never looks at them or logs them.
        """
        terminal_id = text_field(frame, "terminal_id")
        target = text_field(frame, "to")
        if not terminal_id or not target:
            return
        gone = kind == TERMINAL_EXITED
        async with self._lock:
            connection = self._apps.get(target)
        owner = await self.device_owner(device.device_id)
        if connection is None or not owner or connection.username != owner:
            if gone:
                self._terminals.release(device.device_id, terminal_id)
            else:
                self._detach_terminal(device, terminal_id)
            return
        if gone:
            self._terminals.release(device.device_id, terminal_id)
        outgoing = {key: value for key, value in frame.items() if key != "to"}
        outgoing["device_id"] = device.device_id
        await self._send_app(connection, outgoing)

    def _detach_terminal(self, device: DeviceConnection, terminal_id: str) -> None:
        """Tell the device to stop streaming a terminal nobody is holding (§7.3).

        Once per terminal, not once per frame: a shell that keeps printing after its app socket
        closed would otherwise become one gateway request per 16 ms of output. Off the read loop
        as a task, because the request waits for the device's reply and that loop dispatches one
        frame at a time.
        """
        if not self._terminals.note_detached(device.device_id, terminal_id):
            return
        self._spawn_notification(self._run_detach(device, terminal_id))

    async def _run_detach(self, device: DeviceConnection, terminal_id: str) -> None:
        reply = await self._ask_device(
            device, {"type": TERMINAL_DETACH, "terminal_id": terminal_id}
        )
        if reply is None or reply.get("ok") is not True:
            log.info("terminal detach was not acknowledged", device_id=device.device_id)

    async def _detach_terminals_of(self, connection: AppConnection) -> None:
        """An app socket closed: its shells run on, detached, for the device's ten minutes."""
        for device_id, terminal_id in self._terminals.release_connection(connection.id):
            device = self._devices.get(device_id)
            if device is None:
                self._terminals.release(device_id, terminal_id)
                continue
            self._detach_terminal(device, terminal_id)

    def _note_terminal_reply(self, device_id: str, pending: _Pending, frame: Frame) -> str:
        """Read a terminal's holder out of the device's answer, or forget a closed one.

        ``open`` and ``attach`` are the only two frames that say who receives a terminal's
        output, and an `attach` from another socket of the same account simply moves it here.
        Returns the terminal now held, so a reply for a socket that has since closed can be
        undone by the caller.
        """
        if frame.get("ok") is not True:
            return ""
        if pending.kind in (TERMINAL_OPEN, TERMINAL_ATTACH):
            result = object_field(frame, "result") or {}
            terminal_id = text_field(result, "terminal_id") or pending.terminal_id
            if terminal_id:
                self._terminals.hold(device_id, terminal_id, pending.connection_id)
            return terminal_id
        if pending.kind == TERMINAL_CLOSE and pending.terminal_id:
            self._terminals.release(device_id, pending.terminal_id)
        return ""

    # ---- app side ----

    async def attach_app(self, connection: AppConnection) -> None:
        """Take one app socket, closing this account's oldest if it is already at the cap.

        Nothing else bounds them: the upgrade is not rate limited, every socket costs a queue and
        a sender task, and `broadcast_user` and `_publish_event` walk them all for every device
        update and every session event, so one account's sockets are everybody's fan-out cost.
        """
        async with self._lock:
            self._apps[connection.id] = connection
            mine = [item for item in self._apps.values() if item.username == connection.username]
            surplus = mine[: max(0, len(mine) - MAX_APPS_PER_USER)]
            for stale in surplus:
                del self._apps[stale.id]
        for stale in surplus:
            log.info(
                "app socket replaced: account at the connection cap",
                connection=stale.id,
                cap=MAX_APPS_PER_USER,
            )
            await stale.stop(code=CLOSE_TOO_MANY_APPS, reason=APP_REPLACED_CLOSE_REASON)

    async def detach_app(self, connection: AppConnection) -> None:
        async with self._lock:
            self._apps.pop(connection.id, None)
        for key in [key for key in self._pending if key[0] == connection.id]:
            pending = self._pending.pop(key, None)
            if pending is not None and pending.timer is not None:
                pending.timer.cancel()
        await self._detach_terminals_of(connection)

    async def app_hello_payload(
        self,
        account: UserRecord,
        gateway_version: str,
        *,
        stt: Frame,
        polish: Frame,
        apps: Frame,
        preferences: Frame,
    ) -> Frame:
        """The first frame of `/ws/app`: this account's devices and their sessions, nothing else."""
        records = await self.device_store.list_for_user(account.username)
        device_ids = []
        devices = []
        for record in records:
            self._remember_owner(record.device_id, record.username)
            device_ids.append(record.device_id)
            devices.append(
                device_view(
                    record,
                    online=self.device_online(record.device_id),
                    latency_ms=self.latency_for(record.device_id),
                )
            )
        return {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "gateway_version": gateway_version,
            "user": user_view(account),
            "devices": devices,
            "sessions": await self.index.list_sessions(device_ids=device_ids),
            "stt": stt,
            "polish": polish,
            # A31: repeated here so a gateway upgraded under a connected app is caught at the
            # next connection, not only before sign-in.
            "apps": apps,
            # A35: the account's switches, so an app draws them without a second round trip and
            # an app on a gateway that predates them sees none and says so.
            "preferences": preferences,
            "server_time": _now_ms(),
        }

    async def handle_app_frame(self, connection: AppConnection, frame: Frame) -> None:
        kind = frame_type(frame)
        if kind == "pong":
            connection.note_pong()
            return
        connection.note_activity()
        if kind == "session.subscribe":
            await self._handle_subscribe(connection, frame)
            return
        if kind == "session.unsubscribe":
            connection.unsubscribe(text_field(frame, "session_id"))
            return
        if kind == "session.seen":
            await self._handle_seen(connection, frame)
            return
        if kind in FORWARDED_TYPES:
            await self._forward(connection, frame)
            return
        identifier = request_id(frame)
        if identifier:
            await self._send_app(
                connection, error_reply(identifier, ERROR_BAD_REQUEST, f"unsupported type {kind!r}")
            )
        else:
            log.debug("unknown app frame dropped", kind=kind)

    async def broadcast_user(self, username: str, frame: Frame) -> None:
        """Publish to one account's sockets. Every device-derived frame goes through here (A24)."""
        if not username:
            return
        async with self._lock:
            connections = [item for item in self._apps.values() if item.username == username]
        for connection in connections:
            await self._send_app(connection, frame)

    async def send_to_devices(self, username: str, frame: Frame) -> None:
        """Publish to every connected device of one account (A35's `preferences` frame).

        Not taken through the send lock: that lock exists to linearise large forwarded payloads
        against slot changes, and holding it for a frame of two fields would put an account's
        switch behind somebody else's attachment upload.
        """
        if not username:
            return
        async with self._lock:
            connections = list(self._devices.values())
        for connection in connections:
            if await self.device_owner(connection.device_id) != username:
                continue
            try:
                await connection.send(frame)
            except (SlowClientError, ConnectionError) as exc:
                log.warning(
                    "frame to device failed",
                    device_id=connection.device_id,
                    kind=frame_type(frame),
                    error=str(exc),
                )
                await self._drop_device(connection)

    # ---- pairing progress ----

    async def pairing_started(self, code: str, username: str) -> None:
        key = normalize_pairing_code(code)
        async with self._lock:
            self._expire_pairings()
            if len(self._pairings) >= MAX_TRACKED_PAIRINGS:
                oldest = min(self._pairings.values(), key=lambda item: item.created_at)
                self._pairings.pop(normalize_pairing_code(oldest.code), None)
            self._pairings[key] = _Pairing(
                code=code, created_at=time.monotonic(), username=username
            )
        await self.broadcast_user(
            username, {"type": "pairing.progress", "code": code, "step": "waiting"}
        )

    async def pairing_cancelled(self, code: str) -> None:
        async with self._lock:
            self._pairings.pop(normalize_pairing_code(code), None)

    async def pairing_enrolled(self, code: str, device_id: str) -> None:
        """A device redeemed ``code``; report progress under the code as it was minted."""
        key = normalize_pairing_code(code)
        owner = await self.device_owner(device_id)
        async with self._lock:
            pairing = self._pairings.get(key)
            if pairing is None:
                pairing = _Pairing(code=code, created_at=time.monotonic())
                self._pairings[key] = pairing
            pairing.device_id = device_id
            # The enrolled device carries the owner even when the gateway never saw the code
            # minted, which is what a restart between minting and redemption looks like.
            pairing.username = pairing.username or owner
            pairing.steps.add("enrolled")
            display, username = pairing.code, pairing.username
        await self._emit_pairing(display, "enrolled", device_id, username)

    def _expire_pairings(self) -> None:
        cutoff = time.monotonic() - PAIRING_TRACK_SECONDS
        for key in [k for k, item in self._pairings.items() if item.created_at < cutoff]:
            del self._pairings[key]

    async def _advance_pairing(self, device_id: str, step: str) -> None:
        async with self._lock:
            self._expire_pairings()
            pairing = next(
                (item for item in self._pairings.values() if item.device_id == device_id), None
            )
            if pairing is None or step in pairing.steps:
                return
            pairing.steps.add(step)
            display, username = pairing.code, pairing.username
            if step == "agents":
                self._pairings.pop(normalize_pairing_code(display), None)
        await self._emit_pairing(display, step, device_id, username)

    async def _emit_pairing(self, code: str, step: str, device_id: str, username: str) -> None:
        frame: Frame = {"type": "pairing.progress", "code": code, "step": step}
        record = await self.device_store.get(device_id)
        if record is not None:
            frame["device"] = device_view(
                record,
                online=self.device_online(device_id),
                latency_ms=self.latency_for(device_id),
            )
        await self.broadcast_user(username or await self.device_owner(device_id), frame)

    # ---- forwarding ----

    async def _forward(self, connection: AppConnection, frame: Frame) -> None:
        identifier = request_id(frame)
        if not identifier:
            log.debug("forwardable frame without id dropped", kind=frame_type(frame))
            return
        kind = frame_type(frame)
        oversized = _attachments_over_limit(frame)
        if oversized is not None:
            await self._send_app(connection, error_reply(identifier, ERROR_TOO_LARGE, oversized))
            return
        outgoing = dict(frame)
        if kind in FORWARDED_BY_DEVICE:
            device_id = text_field(frame, "device_id")
            if not device_id:
                await self._send_app(
                    connection, error_reply(identifier, ERROR_BAD_REQUEST, "device_id is required")
                )
                return
            if not await self._reaches(connection, device_id):
                await self._send_app(
                    connection, error_reply(identifier, ERROR_NOT_FOUND, "unknown device")
                )
                return
        else:
            session_id = text_field(frame, "session_id")
            # Only the owning device is needed to route, and that is in memory: reading the whole
            # summary back from SQLite would put a disk read in front of every message an app sends.
            owner = await self._owner_of(session_id) if session_id else None
            if owner is None or not await self._reaches(connection, owner):
                await self._send_app(
                    connection, error_reply(identifier, ERROR_NOT_FOUND, "unknown session")
                )
                return
            device_id = owner
            outgoing["device_id"] = device_id
        outgoing["from"] = connection.id

        # Outside the send lock: a device inside its grace period is waited for, and holding the
        # lock across that would stall every other device's forwarding for the whole period.
        await self._await_device(device_id)
        payload = _payload_bytes(frame)
        async with self._send_lock:
            async with self._lock:
                device = self._devices.get(device_id)
                still_connected = self._apps.get(connection.id) is connection
            if not still_connected:
                return
            if device is None:
                await self._send_app(
                    connection,
                    error_reply(identifier, ERROR_DEVICE_OFFLINE, "device is not connected"),
                )
                return
            if not self._budget.claim(payload):
                log.warning(
                    "large forward refused: no room in the in-flight budget",
                    device_id=device_id,
                    bytes=payload,
                    held=self._budget.held,
                )
                await self._send_app(
                    connection, error_reply(identifier, ERROR_TOO_LARGE, FORWARD_BUSY_MESSAGE)
                )
                return
            try:
                # The connection owns the release from here: it gives the budget back when the
                # frame is written, dropped with the queue, or refused for want of room.
                await device.send(outgoing, release=partial(self._budget.release, payload))
            except (SlowClientError, ConnectionError) as exc:
                log.warning("forward to device failed", device_id=device_id, error=str(exc))
                await self._drop_device(device)
                await self._send_app(
                    connection,
                    error_reply(identifier, ERROR_DEVICE_OFFLINE, "device link broken"),
                )
                return
        self._track_pending(
            connection, identifier, device_id, kind, text_field(frame, "terminal_id")
        )

    def _track_pending(
        self,
        connection: AppConnection,
        identifier: str,
        device_id: str,
        kind: str,
        terminal_id: str = "",
    ) -> None:
        key = (connection.id, identifier)
        existing = self._pending.get(key)
        if existing is not None and existing.timer is not None:
            existing.timer.cancel()
        pending = _Pending(
            device_id=device_id,
            connection_id=connection.id,
            kind=kind,
            terminal_id=terminal_id,
        )
        self._pending[key] = pending
        pending.timer = asyncio.create_task(self._expire_pending(key, identifier))

    async def _expire_pending(self, key: tuple[str, str], identifier: str) -> None:
        try:
            await asyncio.sleep(self.request_timeout)
        except asyncio.CancelledError:
            return
        if self._pending.pop(key, None) is None:
            return
        async with self._lock:
            connection = self._apps.get(key[0])
        if connection is not None:
            await self._send_app(
                connection, error_reply(identifier, ERROR_TIMEOUT, "device did not reply in time")
            )

    async def _route_reply(self, device: DeviceConnection, frame: Frame) -> None:
        identifier = request_id(frame)
        target = text_field(frame, "from")
        if not identifier or not target:
            return
        if target == GATEWAY_ORIGIN_ID:
            waiting = self._gateway_pending.pop(identifier, None)
            if waiting is None:
                return
            # The gateway's own `device.update` moves the device exactly as an app's does (A36),
            # and it does so here so that an `update.failed` arriving right behind the acceptance
            # is applied after it, not before.
            if waiting.kind == DEVICE_UPDATE and frame.get("ok") is True:
                await self._begin_update(device.device_id)
            if not waiting.waiter.done():
                waiting.waiter.set_result(frame)
            return
        key = (target, identifier)
        pending = self._pending.get(key)
        if pending is None:
            log.debug("late reply dropped", device_id=device.device_id)
            return
        if pending.device_id != device.device_id:
            # A device answering a request forwarded elsewhere would forge results for a session
            # it does not own. The entry is inspected before it is claimed, so a forged reply
            # cannot consume the request either: the real device can still answer, and the
            # timeout still fires if it never does.
            log.warning("reply from the wrong device dropped", device_id=device.device_id)
            return
        del self._pending[key]
        if pending.timer is not None:
            pending.timer.cancel()
        if pending.kind == DEVICE_UPDATE and frame.get("ok") is True:
            await self._begin_update(device.device_id)
        # A38: an accepted open or attach is where the gateway learns who receives this
        # terminal's output, and an accepted close is where it forgets.
        held = self._note_terminal_reply(device.device_id, pending, frame)
        async with self._lock:
            connection = self._apps.get(target)
        if connection is None:
            # The socket that asked for the terminal closed before its reply arrived, so nobody
            # will ever read it: detach now rather than after a first output frame nobody takes.
            if held:
                self._terminals.release(device.device_id, held)
                self._detach_terminal(device, held)
            return
        reply = {key_: value for key_, value in frame.items() if key_ != "from"}
        if pending.kind in SESSION_RESULT_TYPES:
            reply = await self._reply_with_mark(device.device_id, reply)
        await self._send_app(connection, reply)

    async def _fail_pending_for_device(self, device_id: str, code: str) -> None:
        for key in [key for key, item in self._pending.items() if item.device_id == device_id]:
            pending = self._pending.pop(key, None)
            if pending is None:
                continue
            if pending.timer is not None:
                pending.timer.cancel()
            async with self._lock:
                connection = self._apps.get(key[0])
            if connection is not None:
                await self._send_app(
                    connection, error_reply(key[1], code, "device is not connected")
                )

    # ---- subscriptions and events ----

    async def _handle_subscribe(self, connection: AppConnection, frame: Frame) -> None:
        identifier = request_id(frame)
        session_id = text_field(frame, "session_id")
        if not identifier or not session_id:
            if identifier:
                await self._send_app(
                    connection, error_reply(identifier, ERROR_BAD_REQUEST, "session_id is required")
                )
            return
        indexed = await self.index.get(session_id)
        # A session on another account's device is answered exactly as one that does not exist,
        # so a subscribe cannot be used to probe which session ids are real (A24).
        if indexed is None or not await self._reaches(connection, indexed.device_id):
            await self._send_app(
                connection, error_reply(identifier, ERROR_NOT_FOUND, "unknown session")
            )
            return
        since_seq = int_field(frame, "since_seq")
        buffer = self._buffers.get(session_id)
        if buffer is None:
            events: list[Frame] = []
            resync = since_seq is not None and since_seq < indexed.last_seq
        else:
            events, resync = buffer.replay_from(since_seq)
        cursor = max(since_seq or 0, 0)
        if events:
            cursor = int_field(events[-1], "seq") or cursor
        if resync:
            cursor = indexed.last_seq
        connection.subscribe(session_id, cursor)
        result: dict[str, Any] = {
            "session": indexed.summary,
            "events": events,
            "resync": resync,
        }
        queued = self._queues.get(session_id)
        if queued is not None:
            result["queue"] = {"pending": queued.get("pending") or []}
        await self._send_app(connection, ok_reply(identifier, result))

    async def _claim_session(self, device_id: str, session_id: str) -> bool:
        """True when `device_id` owns `session_id`, claiming it the first time it is seen.

        The binding comes from the device token, never from a frame, and is never rebound: a
        device that announces another machine's session is refused, so it cannot hijack its
        routing, forge its timeline or delete it.
        """
        owner = await self._owner_of(session_id)
        if owner is None:
            self._remember_session_owner(session_id, device_id)
            return True
        if owner == device_id:
            return True
        log.warning(
            "device frame for a session it does not own",
            device_id=device_id,
            owner=owner,
        )
        return False

    async def _owner_of(self, session_id: str) -> str | None:
        """The device that owns a session, from memory whenever this process has seen it.

        Every session the gateway has already routed a frame for is in ``_owners``, so the common
        case answers without a disk read; only one first seen by an earlier process needs one.
        """
        owner = self._owners.get(session_id)
        if owner is not None:
            # Touched, so the coldest entry is the one eviction takes.
            self._owners[session_id] = self._owners.pop(session_id)
            return owner
        owner = await self.index.owner(session_id)
        if owner is not None:
            self._remember_session_owner(session_id, owner)
        return owner

    def _remember_session_owner(self, session_id: str, device_id: str) -> None:
        while len(self._owners) >= MAX_TRACKED_SESSION_OWNERS:
            del self._owners[next(iter(self._owners))]
        self._owners[session_id] = device_id

    def _remember_queue(self, session_id: str, event: Frame) -> None:
        self._queues.pop(session_id, None)
        while len(self._queues) >= MAX_TRACKED_QUEUES:
            del self._queues[next(iter(self._queues))]
        self._queues[session_id] = event

    # ---- account ownership (A24) ----

    async def device_owner(self, device_id: str) -> str:
        """The account a device belongs to, from memory after the first lookup. ``""`` if gone."""
        owner = self._device_owners.get(device_id)
        if owner is not None:
            return owner
        record = await self.device_store.get(device_id)
        if record is None:
            return ""
        self._remember_owner(device_id, record.username)
        return record.username

    def _remember_owner(self, device_id: str, username: str) -> None:
        if device_id in self._device_owners:
            return
        while len(self._device_owners) >= MAX_TRACKED_OWNERS:
            del self._device_owners[next(iter(self._device_owners))]
        self._device_owners[device_id] = username

    async def _reaches(self, connection: AppConnection, device_id: str) -> bool:
        """Whether this socket's account owns the device a request or subscribe named."""
        return await self.device_owner(device_id) == connection.username

    def _buffer_for(self, session_id: str) -> ReplayBuffer:
        buffer = self._buffers.get(session_id)
        if buffer is not None:
            self._buffers[session_id] = self._buffers.pop(session_id)
            return buffer
        while len(self._buffers) >= MAX_REPLAY_BUFFERS:
            coldest, _ = next(iter(self._buffers.items()))
            self._buffers.pop(coldest, None)
            log.info("replay buffer evicted", sessions=len(self._buffers))
        buffer = ReplayBuffer()
        self._buffers[session_id] = buffer
        return buffer

    async def _handle_session_event(self, device: DeviceConnection, frame: Frame) -> None:
        session_id = text_field(frame, "session_id")
        event = object_field(frame, "event")
        if not session_id or event is None:
            return
        if not await self._claim_session(device.device_id, session_id):
            return
        await self._publish_event(device, session_id, event, frame)

    async def _publish_event(
        self, device: DeviceConnection, session_id: str, event: Frame, frame: Frame
    ) -> bool:
        """Buffer one event and fan it out. Returns False for a dropped or duplicate event."""
        seq = int_field(event, "seq")
        if seq is None:
            return False
        size = len(encode(frame).encode("utf-8"))
        if size > MAX_EVENT_BYTES:
            log.warning(
                "oversized event dropped",
                device_id=device.device_id,
                kind=text_field(event, "kind"),
                bytes=size,
            )
            return False
        buffer = self._buffer_for(session_id)
        if not buffer.append(event, size):
            return False
        if text_field(event, "kind") == "queue":
            # A6: `queue` describes current state rather than timeline history, so the newest
            # snapshot is kept for `session.subscribe` instead of being replayed from the buffer.
            self._remember_queue(session_id, event)
        # Recorded before the fan-out and without touching the disk, so no app can read a summary
        # that predates an event it has already been sent, and no event waits on SQLite.
        self.index.record_seq(session_id, seq)
        # A5: apps address a session by (device_id, session_id) and a pushed event has to carry
        # the device identity the gateway derived from the token, not one the frame claimed.
        outgoing = {**frame, "device_id": device.device_id}
        async with self._lock:
            subscribers = [item for item in self._apps.values() if item.subscribed(session_id)]
        for subscriber in subscribers:
            subscriber.subscribe(session_id, seq)
            await self._send_app(subscriber, outgoing)
        if text_field(event, "kind") == "resume":
            # A35: a pending resume changes no session state, so this event is the only thing
            # that can tell someone their session was paused, resumed or given up on.
            await self._notify_resume(device, session_id, event)
        return True

    # ---- session index ----

    async def _store_session(self, device_id: str, summary: Frame) -> None:
        stored = dict(summary)
        stored["device_id"] = device_id
        session_id = text_field(stored, "session_id")
        if not session_id or not await self._claim_session(device_id, session_id):
            return
        previous = await self.index.get(session_id)
        indexed = await self.index.upsert(stored)
        if indexed is None:
            log.warning("session summary refused", device_id=device_id)
            return
        owner = await self.device_owner(device_id)
        await self.broadcast_user(owner, {"type": "session.updated", "session": indexed.summary})
        if indexed.unseen != (previous is not None and previous.unseen):
            self._badge_changed(owner, indexed)
        # A36: an automatic update a busy device refused is asked again the moment it goes quiet.
        await self._auto_update.on_session_change(device_id)
        # A session the gateway has never seen has no transition to report: an index that was
        # just created (or a device reconnecting after a wipe) must not produce a push storm.
        if self._on_session_transition is None or previous is None:
            return
        if previous.state == indexed.state:
            return
        self._notify_transition(previous.state, indexed, owner)

    def _notify_transition(self, previous_state: str, indexed: IndexedSession, owner: str) -> None:
        """Run the push hook off the device read loop.

        A push is an outbound HTTP call to a browser vendor or to APNs. Awaiting it here would
        hold every later frame from the same device behind it, because that loop reads and
        dispatches one frame at a time. Whether an app is watching is decided now, at the
        transition, so the delivery running later cannot change the outcome.
        """
        hook = self._on_session_transition
        if hook is None:
            return
        transition = SessionTransition(
            previous_state=previous_state,
            state=indexed.state,
            session=indexed.summary,
            owner=owner,
            has_active_subscriber=self._has_active_subscriber(indexed.session_id),
        )
        self._spawn_notification(self._run_transition(hook, transition))

    async def _run_transition(self, hook: TransitionHook, transition: SessionTransition) -> None:
        try:
            await hook(transition)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "session transition hook failed",
                session_id=text_field(transition.session, "session_id"),
            )

    async def _notify_resume(self, device: DeviceConnection, session_id: str, event: Frame) -> None:
        """Cue a push from a ``resume`` event (A35, §3.7).

        The device decided; the gateway only announces. Run off the read loop for the reason
        `_notify_transition` gives, and with the same rule about an app that is already watching.
        """
        hook = self._on_session_resume
        if hook is None:
            return
        notice = SessionResumeNotice(
            status=text_field(event, "status"),
            session_id=session_id,
            device_id=device.device_id,
            owner=await self.device_owner(device.device_id),
            has_active_subscriber=self._has_active_subscriber(session_id),
        )
        self._spawn_notification(self._run_resume(hook, notice))

    async def _run_resume(self, hook: ResumeHook, notice: SessionResumeNotice) -> None:
        try:
            await hook(notice)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session resume hook failed", session_id=notice.session_id)

    def _spawn_notification(self, work: Coroutine[Any, Any, None]) -> None:
        """Run work off the read loop as a task the shutdown drains.

        Push triggers and A38's ``terminal.detach`` both belong here: each would otherwise hold
        the next frame from the same device behind an outbound call.
        """
        task = asyncio.create_task(work)
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)

    async def _forget_session(self, device_id: str, session_id: str) -> None:
        if not await self._claim_session(device_id, session_id):
            return
        removed = await self.index.remove(session_id)
        owner = await self.device_owner(device_id)
        await self._drop_session(device_id, session_id, owner)
        if removed is not None and removed.unseen:
            self._badge_changed(owner, removed)

    async def forget_device_sessions(self, device_id: str, owner: str) -> None:
        """Drop every session of a deleted device from the index and from the apps.

        The caller names the owner: the device's record is already gone by now, so a lookup here
        would find nobody to tell.
        """
        removed = await self.index.remove_for_device(device_id)
        for indexed in removed:
            await self._drop_session(device_id, indexed.session_id, owner)
        marked = [indexed for indexed in removed if indexed.unseen]
        if marked:
            self._badge_changed(owner, marked[-1])
        self._terminals.release_device(device_id)
        self._device_owners.pop(device_id, None)

    async def _drop_session(self, device_id: str, session_id: str, owner: str) -> None:
        self._buffers.pop(session_id, None)
        self._queues.pop(session_id, None)
        self._owners.pop(session_id, None)
        async with self._lock:
            for connection in self._apps.values():
                connection.unsubscribe(session_id)
        await self.broadcast_user(
            owner, {"type": "session.removed", "session_id": session_id, "device_id": device_id}
        )

    def _has_active_subscriber(self, session_id: str) -> bool:
        """True when an app is watching this session and its user did something recently.

        Heartbeat traffic deliberately does not count as activity: a phone with a backgrounded
        tab still answers ``ping``, and suppressing its notification would defeat the feature.
        """
        return any(
            connection.subscribed(session_id) and connection.idle_for() <= ACTIVE_APP_WINDOW_SECONDS
            for connection in self._apps.values()
        )

    # ---- the A47 mark ----

    async def _handle_seen(self, connection: AppConnection, frame: Frame) -> None:
        """The person has this conversation in front of them, so its mark goes (A47, §6.2).

        The gateway's to answer, and idempotent: a session without the mark is answered `{}` and
        nobody else hears of it. Another account's session is `not_found`, as one that does not
        exist would be (A24).
        """
        identifier = request_id(frame)
        session_id = text_field(frame, "session_id")
        if not identifier or not session_id:
            if identifier:
                await self._send_app(
                    connection, error_reply(identifier, ERROR_BAD_REQUEST, "session_id is required")
                )
            return
        owner = await self._owner_of(session_id)
        if owner is None or not await self._reaches(connection, owner):
            await self._send_app(
                connection, error_reply(identifier, ERROR_NOT_FOUND, "unknown session")
            )
            return
        cleared = await self.index.clear_unseen(session_id)
        await self._send_app(connection, ok_reply(identifier, {}))
        if cleared is None:
            return
        await self.broadcast_user(
            connection.username, {"type": "session.updated", "session": cleared.summary}
        )
        self._badge_changed(connection.username, cleared)

    def _badge_changed(self, owner: str, indexed: IndexedSession) -> None:
        """One of the account's sessions gained or lost its mark, so the account's count moved."""
        if self._on_badge_change is None or not owner:
            return
        self._on_badge_change(
            BadgeChange(owner=owner, device_id=indexed.device_id, session_id=indexed.session_id)
        )

    async def _reply_with_mark(self, device_id: str, reply: Frame) -> Frame:
        """Mark the session a device's reply carries as the gateway has it (A47).

        A device never sends the mark, and an app may draw the reply's session in place of the
        copy it holds, so a marked row would otherwise lose its dot until the next update. The
        reply never adds a mark the index does not hold; it drops one the summary itself ends,
        as archiving or the session working again does, so that row and list agree once the
        device publishes the same summary.
        """
        result = object_field(reply, "result")
        session = object_field(result, "session") if result is not None else None
        if result is None or session is None:
            return reply
        stored = await self.index.get(text_field(session, "session_id"))
        unseen = (
            stored is not None
            and stored.device_id == device_id
            and stored.unseen
            and next_unseen(stored.state, True, session)
        )
        return {**reply, "result": {**result, "session": with_mark(dict(session), unseen)}}

    # ---- helpers ----

    async def _announce_device(self, connection: DeviceConnection) -> None:
        record = await self.device_store.get(connection.device_id)
        if record is None:
            return
        self._remember_owner(record.device_id, record.username)
        await self.broadcast_user(
            record.username,
            {
                "type": "device.updated",
                "device": device_view(
                    record, online=True, latency_ms=connection.latency_ms, last_seen=_now_ms()
                ),
            },
        )

    async def _announce_stored_device(self, device_id: str) -> None:
        """Publish a device the gateway changed itself, with its connection state as it is now."""
        record = await self.device_store.get(device_id)
        if record is None:
            return
        self._remember_owner(record.device_id, record.username)
        await self.broadcast_user(
            record.username,
            {
                "type": "device.updated",
                "device": device_view(
                    record,
                    online=self.device_online(device_id),
                    latency_ms=self.latency_for(device_id),
                ),
            },
        )

    async def _send_app(self, connection: AppConnection, frame: Frame) -> None:
        try:
            await connection.send(frame)
        except SlowClientError:
            log.warning("app dropped: send queue exhausted", connection=connection.id)
            await self._drop_app(connection)
        except ConnectionError:
            await self._drop_app(connection)

    async def _drop_app(self, connection: AppConnection) -> None:
        async with self._lock:
            if self._apps.get(connection.id) is connection:
                del self._apps[connection.id]
        await connection.stop(code=CLOSE_SLOW_CLIENT, reason="slow client")

    async def _drop_device(self, connection: DeviceConnection) -> None:
        self._auto_update.forget(connection)
        async with self._lock:
            if self._devices.get(connection.device_id) is connection:
                del self._devices[connection.device_id]
        await connection.stop(code=CLOSE_SLOW_CLIENT, reason="slow client")

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_TICK_SECONDS)
                await self.heartbeat_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat tick failed")

    async def heartbeat_tick(self) -> None:
        """Ping idle connections and close the ones that stopped answering."""
        now = time.monotonic()
        async with self._lock:
            connections = [*self._devices.values(), *self._apps.values()]
        for connection in connections:
            if connection.silent_for() > SILENT_TIMEOUT_SECONDS:
                log.info("closing silent connection", connection=connection.id)
                await connection.stop(code=1001, reason="no frames received")
                continue
            if now - connection.last_ping_at >= PING_INTERVAL_SECONDS:
                connection.note_ping_sent()
                try:
                    await connection.send({"type": "ping"})
                except (SlowClientError, ConnectionError):
                    await connection.stop(code=CLOSE_SLOW_CLIENT, reason="slow client")


def _attachments_over_limit(frame: Frame) -> str | None:
    """Enforce the §5 attachment bounds, so a device never has to defend itself against them."""
    attachments = frame.get("attachments")
    if not isinstance(attachments, list):
        return None
    if len(attachments) > MAX_ATTACHMENTS:
        return f"at most {MAX_ATTACHMENTS} attachments per message"
    for item in attachments:
        if not isinstance(item, dict):
            continue
        encoded = item.get("data_base64")
        # 4 base64 characters carry 3 bytes; compare without decoding megabytes to count them.
        if isinstance(encoded, str) and len(encoded) // 4 * 3 > MAX_ATTACHMENT_BYTES:
            return f"attachments are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB each"
    return None


def _payload_bytes(frame: Frame) -> int:
    """How much attachment payload one forwarded frame carries, without copying any of it.

    Only the base64 strings can be large, and their lengths are known in O(1), so the in-flight
    budget is measured without materialising the encoded frame a second time.
    """
    attachments = frame.get("attachments")
    if not isinstance(attachments, list):
        return 0
    total = 0
    for item in attachments:
        if isinstance(item, dict):
            encoded = item.get("data_base64")
            if isinstance(encoded, str):
                total += len(encoded)
    return total


def _agent_list(value: Any) -> list[dict[str, Any]]:
    """The agents a device announced, bounded exactly as ``POST /api/devices/enroll`` bounds them.

    The blob is stored, re-broadcast inside `device.updated` to every app socket of the account
    and returned by `GET /api/devices`, so an unbounded list on this path would reach an app queue
    that drops the socket at 16 MiB. A gateway limit, not a wire rule: the protocol says nothing
    about how many agents a machine may have.
    """
    if not isinstance(value, list):
        return []
    kept = []
    for item in value[:MAX_ANNOUNCED_AGENTS]:
        if isinstance(item, dict) and len(encode(item)) <= MAX_AGENT_ENTRY_BYTES:
            kept.append(item)
    return kept


def _now_ms() -> int:
    return int(time.time() * 1000)
