"""One Codex thread on the shared daemon, driven as a session runner.

Everything the terminal does on this thread reaches us as ordinary
notifications, and everything we do reaches the terminal, so the only
difference from the private app-server adapter is that we neither own nor
outlive the process on the other end.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ....attachments import Attachment, describe, discard, materialise, wire_attachments
from ....errors import RcError
from ....logging_setup import logger
from ....models import UNSET, Command, SpeedSetting, now_ms
from ....sessions.channel import SessionChannel
from ....sessions.limits import LimitStop, codex_limit_windows
from .. import commands as slash
from ..account import LIMITS_TIMEOUT, RATE_LIMITS
from ..echoes import Echo, EchoLog
from ..models import ModelCatalog, tier_id
from ..reports import ThreadFacts
from ..translate import CodexTranslator, item_type, normalise, text_of
from .children import ChildWatch, child_states
from .dialogs import DialogDesk
from .rpc import DaemonClient

log = logger("rc_client.codex.daemon.session")

APPROVAL_TIMEOUT = 300.0
OUTPUT_THROTTLE = 0.2
DRAIN_TIMEOUT = 15.0
# How long a close waits for the interrupted turn to settle before archiving
# the thread anyway (A39). Shorter than `DRAIN_TIMEOUT`: the person is closing
# the session, so the archive matters more than the last words of the turn.
CLOSE_DRAIN_TIMEOUT = 5.0
BACKFILL_ITEMS = 200
BACKFILL_PAGE = 100
# Give the daemon's full completion notification precedence over its idle status.
IDLE_SETTLE_DELAY = 0.05
COMPLETED_TURNS = 128
# Notifications that mean something has moved on this thread, so a resume the
# daemon refused before may be accepted now.
TURN_BOUNDARIES = frozenset({"turn/started", "turn/completed", "thread/status/changed"})


ControlCallback = Callable[[], Awaitable[None]]
TurnEndCallback = Callable[[], Awaitable[None]]
ThreadIdCallback = Callable[[str], Awaitable[None]]


def archived_refusal(message: str) -> bool:
    """Whether Codex refused a resume because the thread is archived (A39).

    Codex answers "session <id> is archived. Run `codex unarchive <id>` …", and
    the word is the only signal there is: the error code is the same one an
    unresumable young thread gets.
    """
    return "archived" in message.lower()


@dataclass(slots=True)
class HeldTurn:
    """A parent turn that ended while the subagents it started were still working.

    Working means all of it: the turn is not over, so none of what its end
    would carry is published until the last child has finished.
    """

    stop_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    limit: Any = None


class CodexDaemonSession:
    """A session runner backed by one thread inside the shared daemon."""

    agent = "codex"

    def __init__(
        self,
        channel: SessionChannel,
        client: DaemonClient,
        *,
        cwd: str,
        catalog: ModelCatalog,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        speed: str | None = None,
        thread_id: str | None = None,
        thread_config: dict[str, Any] | None = None,
        created_here: bool | None = None,
        on_turn_end: TurnEndCallback | None = None,
        on_control_change: ControlCallback | None = None,
        on_thread_id: ThreadIdCallback | None = None,
        on_closing: ThreadIdCallback | None = None,
    ) -> None:
        self.channel = channel
        self._client = client
        self._cwd = cwd
        self._catalog = catalog
        self._model = model
        self._permission_mode = permission_mode or "on-request"
        self._effort = effort
        self._speed = speed
        self._thread_id = thread_id
        self._thread_config = thread_config
        self._sandbox: dict[str, Any] | None = None
        self._on_turn_end = on_turn_end
        self._on_control_change = on_control_change
        self._on_thread_id = on_thread_id
        self._on_closing = on_closing
        self._translator = CodexTranslator(cwd=cwd, mirror_user_messages=True)
        self._turn_id: str | None = None
        self._completed_turn_ids: deque[str] = deque(maxlen=COMPLETED_TURNS)
        self._turn_started_at = 0
        self._interrupting = False
        # The subagents this thread spawned, and the completion its own turn
        # reported while they were still working.
        self._children = ChildWatch()
        self._held: HeldTurn | None = None
        self._turn_epoch = 0
        self._activity_epoch = 0
        self._notification_lock = asyncio.Lock()
        self._start_requests = 0
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._dialogs = DialogDesk(
            channel,
            cwd=cwd,
            busy=lambda: self.busy,
            pending_diff=self._translator.pending_diff,
        )
        self._last_output_flush: dict[str, float] = {}
        self._echoes = EchoLog()
        self._last_item_id: str | None = None
        self._subscribed = False
        # Whether a resume has already been refused for this thread. A thread
        # with no rollout refuses every time, and asking again on each message
        # and each notification puts a failed round trip in front of them.
        self._resume_refused = False
        self._terminal_seen = False
        self._terminal_holds = False
        self._terminal_spoke = False
        self._local_turn = False
        # What started the turn this device is about to run: `remote` for a
        # person's message, `resume` for the scheduler's prompt (A35).
        self._trigger = "remote"
        self._created_here = thread_id is None if created_here is None else created_here

    # ------------------------------------------------------------- accessors

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def terminal_seen(self) -> bool:
        """Whether a terminal has ever been seen in this thread. Never unsaid."""
        return self._terminal_seen

    @property
    def terminal_holds(self) -> bool:
        """Whether a terminal is in this thread now, as far as anyone can tell."""
        return self._terminal_holds

    @property
    def local_turn(self) -> bool:
        """Whether the turn running right now is one this device started."""
        return self._local_turn and self.busy

    @property
    def created_here(self) -> bool:
        return self._created_here

    @property
    def busy(self) -> bool:
        """Whether this session is working: its own turn, or a subagent's."""
        return self._turn_id is not None or self._held is not None or self._start_requests > 0

    @property
    def activity_epoch(self) -> int:
        """Invalidate an index snapshot when live activity overtakes its read."""
        return self._activity_epoch

    @property
    def supports_steer(self) -> bool:
        return True

    def claim_terminal(self) -> None:
        """Record a terminal in this thread from evidence no scan can give.

        A thread another client has just opened or resumed belongs to whoever
        opened it, and the process scan may not see that TUI at all: it can be
        younger than the scan, or running in a directory of its own.
        """
        self._terminal_seen = True
        self._terminal_holds = True
        self._terminal_spoke = True

    def terminal_present(self, present: bool) -> bool:
        """Fold one process scan into what we believe about the terminal.

        Evidence gathered since the last scan outranks the scan: the TUI that
        typed, opened or resumed this thread may be standing in a directory
        where no process the scan can attribute to it is to be found. Answers
        with what the thread now believes, which is what the caller shares out
        between the threads competing for the same terminal.
        """
        if self._terminal_spoke:
            self._terminal_spoke = False
            self._terminal_holds = True
        else:
            self._terminal_holds = present
        return self._terminal_holds

    async def _control_changed(self) -> None:
        if self._on_control_change is not None:
            await self._on_control_change()

    # ------------------------------------------------------------- lifecycle

    @property
    def subscribed(self) -> bool:
        return self._subscribed

    async def start(self) -> None:
        if self._thread_id is None:
            await self._start_thread()
        else:
            await self.resubscribe()

    async def _start_thread(self) -> None:
        params: dict[str, Any] = {"cwd": self._cwd, "approvalPolicy": self._permission_mode}
        if self._model:
            params["model"] = self._model
        if self._thread_config:
            params["config"] = self._thread_config
        wanted_speed = self._speed
        result = await self._client.request("thread/start", params)
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise RcError("agent_unavailable", "the codex daemon did not return a thread id")
        self._thread_id = thread_id
        self._subscribed = True
        self._adopt_settings(result)
        if wanted_speed is not None and wanted_speed != self._speed:
            # `thread/start` accepts `serviceTier` and ignores it: the tier is a
            # thread setting, so a new session asks for it once the thread is up.
            self._speed = wanted_speed
            await self._client.request(
                "thread/settings/update", {"threadId": thread_id, "serviceTier": wanted_speed}
            )
        # A thread whose tier came from the user's own Codex configuration is
        # already faster than the session says it is.
        await self.channel.set_meta(speed=self._speed)
        if self._on_thread_id is not None:
            await self._on_thread_id(thread_id)

    async def try_resume(self) -> bool:
        """Resume, unless we already know the daemon will refuse.

        The refusal is remembered until something could have changed it — a
        turn boundary on this thread, or a turn this device has just started —
        so an unresumable thread costs one failed request per turn rather than
        one per message and one per notification.
        """
        if self._subscribed:
            return True
        if self._resume_refused:
            return False
        return await self.resubscribe()

    def resume_again(self) -> None:
        """Something happened on this thread; a refused resume is worth retrying."""
        self._resume_refused = False

    async def resubscribe(self) -> bool:
        """Resume the thread and catch up on anything we missed while away.

        A thread the terminal created moments ago has no rollout until its first
        turn starts, and the daemon refuses to resume it until then. That is not
        a failure: the thread is loaded, `turn/start` already works on it, and
        the subscription is taken the moment the rollout exists.
        """
        thread_id = self._thread_id
        if thread_id is None:
            return False
        params: dict[str, Any] = {"threadId": thread_id, "excludeTurns": True}
        if self._thread_config and self._created_here:
            # Never rewrite the configuration of a thread somebody else started:
            # a `config` on resume applies to the thread, not to our view of it.
            params["config"] = self._thread_config
        result = await self._resume_thread(thread_id, params)
        if result is None:
            self._subscribed = False
            self._resume_refused = True
            return False
        self._subscribed = True
        self._resume_refused = False
        self._adopt_settings(result)
        await self.backfill()
        return True

    async def _resume_thread(self, thread_id: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """`thread/resume`, unarchiving the thread first when that is what it wants.

        A session the person closed is archived in Codex (A39), and Codex
        refuses to resume an archived thread by name — `codex unarchive` is what
        it tells a person to run. Writing to a closed session is what brings it
        back (A15), so the unarchive belongs to the same step.
        """
        try:
            return await self._client.request("thread/resume", params)
        except RcError as exc:
            if not archived_refusal(exc.message):
                log.info("codex thread not resumable yet", error=exc.message[:120])
                return None
        try:
            await self._client.request("thread/unarchive", {"threadId": thread_id})
            return await self._client.request("thread/resume", params)
        except RcError as exc:
            log.warning("could not unarchive a closed codex thread", error=exc.message[:200])
            return None

    def _adopt_settings(self, result: dict[str, Any]) -> None:
        sandbox = result.get("sandbox")
        if isinstance(sandbox, dict):
            self._sandbox = sandbox
        model = result.get("model")
        if isinstance(model, str) and model:
            self._model = model
        effort = result.get("reasoningEffort")
        if isinstance(effort, str) and effort:
            self._effort = effort
        policy = result.get("approvalPolicy")
        if isinstance(policy, str) and policy:
            self._permission_mode = policy
        if "serviceTier" in result:
            # Null is the standard speed, so the key's absence is the only thing
            # that leaves the tier alone (amendment A21).
            self._speed = tier_id(result.get("serviceTier"))

    async def publish_settings(self) -> None:
        """Publish what the thread reports about itself. The tier may be null (A21)."""
        fields: dict[str, Any] = {"speed": self._speed}
        for key, value in (
            ("model", self._model),
            ("permission_mode", self._permission_mode),
            ("effort", self._effort),
        ):
            if value is not None:
                fields[key] = value
        await self.channel.set_meta(**fields)

    async def close(self) -> None:
        """Let go of the thread without unloading it: the terminal may still be there."""
        self._dialogs.cancel_all()
        thread_id = self._thread_id
        if thread_id is not None and self._subscribed and self._client.connected:
            with contextlib.suppress(RcError):
                await self._client.request("thread/unsubscribe", {"threadId": thread_id})
        self._subscribed = False

    async def shutdown(self) -> None:
        """End the thread for good (A39): stop the turn, archive it, then let go.

        Unsubscribing alone leaves the thread loaded in the daemon with its turn
        still running, and its next word would take the session back out of the
        Archive. Archiving it in Codex is what ends it for every client, so the
        service is told first: nothing the thread says on its way out is a
        reason to adopt or revive it.
        """
        thread_id = self._thread_id
        if thread_id is not None and self._on_closing is not None:
            await self._on_closing(thread_id)
        if self.busy:
            with contextlib.suppress(TimeoutError, RcError):
                await asyncio.wait_for(self.interrupt(), CLOSE_DRAIN_TIMEOUT)
        if thread_id is not None and self._client.connected:
            try:
                await self._client.request("thread/archive", {"threadId": thread_id})
            except RcError as exc:
                log.warning("codex refused to archive a closed thread", error=exc.message[:200])
        await self.close()

    # -------------------------------------------------------------- backfill

    async def backfill(self) -> None:
        """Replay the items appended since the last one we published."""
        thread_id = self._thread_id
        if thread_id is None:
            return
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        stop = self._last_item_id
        found = False
        while len(collected) < BACKFILL_ITEMS and not found:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": BACKFILL_PAGE,
                "sortDirection": "desc",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                page = await self._client.request("thread/items/list", params)
            except RcError as exc:
                log.warning("codex history backfill failed", error=exc.message[:200])
                return
            items = [item for item in (page.get("data") or []) if isinstance(item, dict)]
            for item in items:
                if stop is not None and str(item.get("id") or "") == stop:
                    found = True
                    break
                collected.append(item)
            cursor = page.get("nextCursor") if isinstance(page.get("nextCursor"), str) else None
            if not cursor or not items:
                break
        if stop is None and not collected:
            return
        for item in reversed(collected):
            await self._publish_item(item, completed=True, timestamp=None)

    # ---------------------------------------------------------- notifications

    async def notification(self, method: str, params: dict[str, Any]) -> None:
        self._activity_epoch += 1
        async with self._notification_lock:
            settled, idle = await self._notification(method, params)
            epoch = self._activity_epoch
        if settled:
            await self._after_turn_end()
        if idle:
            await self.reconcile_status(False, epoch)

    async def _notification(self, method: str, params: dict[str, Any]) -> tuple[bool, bool]:
        if method in TURN_BOUNDARIES:
            # A thread that was too young to resume has a rollout once a turn
            # has run in it, so this is where a refusal is worth retrying.
            self.resume_again()
        if not self._subscribed:
            # The thread may have just become resumable; take the subscription
            # before handling anything, so the backfill and the live stream
            # line up.
            await self.try_resume()
        if method == "turn/started":
            await self._turn_started(params)
            return False, False
        if method == "turn/completed":
            return await self._turn_completed(params), False
        if method in {"item/started", "item/completed"}:
            completed = method == "item/completed"
            item = params.get("item")
            if isinstance(item, dict):
                stamp = params.get("completedAtMs" if completed else "startedAtMs")
                await self._publish_item(item, completed, stamp)
                return await self._note_children(item, completed), False
            return False, False
        if method == "thread/settings/updated":
            await self._settings_updated(params)
            return False, False
        if method == "thread/status/changed":
            status = params.get("status")
            if isinstance(status, dict) and status.get("type") == "active":
                await self._status_changed(True)
            return False, isinstance(status, dict) and status.get("type") == "idle"
        for emit in self._translator.notification(method, params):
            await self._apply(emit)
        return False, False

    async def _turn_started(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        if not turn_id or self._turn_id == turn_id:
            return
        self._translator.turn_id = turn_id
        already_started = self._turn_id is not None and self._turn_id.startswith("unknown:")
        self._turn_id = turn_id
        if already_started:
            return
        if self._held is not None:
            # Subagents are holding the turn open, so whatever the parent does
            # next belongs to it: one turn, from the prompt to the last child.
            return
        self._turn_started_at = now_ms()
        self._turn_done.clear()
        await self.channel.begin_turn("terminal" if not self._echoes else self._trigger)

    async def _turn_completed(self, params: dict[str, Any]) -> bool:
        if self._turn_id is None and self._held is None and self._turn_done.is_set():
            return False
        turn = params.get("turn") or {}
        completed_id = str(turn.get("id") or "")
        if completed_id and completed_id in self._completed_turn_ids:
            return False
        if (
            completed_id
            and self._turn_id is not None
            and not self._turn_id.startswith("unknown:")
            and self._turn_id != completed_id
        ):
            return False
        if completed_id:
            # A queued start can be pending without a turn id yet. Its old
            # completion must not finish it or invalidate its start response.
            self._completed_turn_ids.append(completed_id)
        completion: dict[str, Any] = {}
        for emit in self._translator.notification("turn/completed", params):
            if emit.kind == "turn_completed":
                completion = dict(emit.fields)
                continue
            await self._apply(emit)
        reported = str(completion.get("stop_reason") or "completed")
        stop_reason = "interrupted" if self._interrupting else reported
        self._interrupting = False
        self._turn_id = None
        usage = dict(completion.get("usage") or self._translator.usage)
        held = self._held
        if stop_reason != "interrupted" and self._children.working:
            # Working means all of it: the agent is done and its subagents are
            # not, so nothing of the turn's end is published yet.
            self._held = HeldTurn(stop_reason, usage, completion.get("limit"))
            log.info("a codex turn waits for its subagents", agents=self._children.count)
            return False
        reported_ms = int(completion.get("duration_ms") or 0)
        duration = self._elapsed() if held is not None else reported_ms or self._elapsed()
        await self._settle_turn(stop_reason, usage, completion.get("limit"), duration)
        return True

    def _elapsed(self) -> int:
        """How long the whole turn has taken, subagents included."""
        return max(0, now_ms() - self._turn_started_at)

    async def _settle_turn(
        self, stop_reason: str, usage: dict[str, Any], limit: Any, duration: int
    ) -> None:
        """End the turn for good: everything this session started has finished."""
        await self._publish_unread(stop_reason)
        self._held = None
        self._children.forget()
        self._local_turn = False
        self._trigger = "remote"
        self._turn_epoch += 1
        self._turn_done.set()
        self._last_output_flush.clear()
        windows = await self._limit_windows(limit)
        await self.channel.end_turn(stop_reason, duration, usage or None, limit=windows)

    async def _after_turn_end(self) -> None:
        """Queue delivery may wait for more daemon notifications, outside their lock."""
        if self._on_turn_end is not None:
            await self._on_turn_end()
        await self._control_changed()

    # -------------------------------------------------------------- subagents

    async def child_activity(self, child_id: str, active: bool, epoch: int | None = None) -> None:
        """A thread this session spawned started or finished its work."""
        if not child_id:
            return
        async with self._notification_lock:
            if epoch is not None and epoch != self._activity_epoch:
                return
            if epoch is None:
                self._activity_epoch += 1
            settled = await self._child_activity(child_id, active)
        if settled:
            await self._after_turn_end()

    async def _child_activity(self, child_id: str, active: bool) -> bool:
        if self._children.note(child_id, active):
            return await self._release()
        return False

    async def check_children(self) -> None:
        """Let go of subagents that have gone silent, on the scan interval."""
        async with self._notification_lock:
            settled = False
            if self._held is not None and not self._children.working:
                settled = await self._release()
        if settled:
            await self._after_turn_end()

    async def _release(self) -> bool:
        """End the turn the subagents were holding open, now that they are done."""
        held = self._held
        if held is None or self._turn_id is not None:
            return False
        await self._settle_turn(held.stop_reason, held.usage, held.limit, self._elapsed())
        return True

    async def _note_children(self, raw: dict[str, Any], completed: bool) -> bool:
        """Read one live item for what it says about this thread's subagents.

        The daemon may never send us a child's own notifications, but the
        parent's timeline names them: this is where a session learns that work
        it started is still running.
        """
        settled = False
        for child_id, active in child_states(normalise(raw), completed).items():
            settled = await self._child_activity(child_id, active) or settled
        return settled

    async def _limit_windows(self, limit: Any) -> LimitStop | None:
        """Put the window that ran out on a usage-limit stop (amendment A35).

        The turn says only that the limit was reached; the daemon knows both
        windows, and a read that fails leaves the stop without a time rather
        than turning it into an ordinary error.
        """
        if not isinstance(limit, LimitStop):
            return None
        try:
            reply = await asyncio.wait_for(self._client.request(RATE_LIMITS, {}), LIMITS_TIMEOUT)
        except (RcError, TimeoutError):
            return limit
        except Exception as exc:  # a quota read must never cost the turn's end
            log.warning("could not read the Codex quota", error=type(exc).__name__)
            return limit
        return codex_limit_windows(reply)

    async def _settings_updated(self, params: dict[str, Any]) -> None:
        settings = params.get("threadSettings")
        if not isinstance(settings, dict):
            return
        model = settings.get("model")
        effort = settings.get("effort")
        policy = settings.get("approvalPolicy")
        self._model = model if isinstance(model, str) and model else self._model
        self._effort = effort if isinstance(effort, str) and effort else self._effort
        if isinstance(policy, str) and policy:
            self._permission_mode = policy
        if "serviceTier" in settings:
            self._speed = tier_id(settings.get("serviceTier"))
        await self.publish_settings()

    async def _status_changed(self, active: bool) -> None:
        """Catch a turn that was already running when we resumed the thread."""
        if active and self._turn_id is None:
            self._turn_id = f"unknown:{uuid.uuid4()}"
            if self._held is not None:
                # The subagents have kept this turn open; the thread going
                # active again is that same turn carrying on.
                return
            self._turn_started_at = now_ms()
            self._turn_done.clear()
            await self.channel.begin_turn(self._trigger if self._local_turn else "terminal")

    async def reconcile_status(self, active: bool, epoch: int) -> None:
        """Recover a lost boundary without applying an index read older than live work."""
        if not active:
            if self._turn_id is None:
                return
            await asyncio.sleep(IDLE_SETTLE_DELAY)
        async with self._notification_lock:
            if epoch != self._activity_epoch or self._start_requests:
                return
            if active:
                await self._status_changed(True)
                return
            turn_id = self._turn_id
        if turn_id is None or self._thread_id is None:
            return
        try:
            result = await self._client.request(
                "thread/read", {"threadId": self._thread_id, "includeTurns": True}
            )
        except RcError:
            return
        thread = result.get("thread")
        if not isinstance(thread, dict):
            return
        status = thread.get("status")
        if not isinstance(status, dict) or status.get("type") != "idle":
            return
        turns = thread.get("turns") or []
        candidates = [
            turn
            for turn in turns
            if isinstance(turn, dict)
            and (turn_id.startswith("unknown:") or turn.get("id") == turn_id)
        ]
        if not candidates or candidates[-1].get("status") not in {
            "completed",
            "interrupted",
            "failed",
        }:
            return
        async with self._notification_lock:
            if epoch != self._activity_epoch or self._start_requests or self._turn_id != turn_id:
                return
            settled = await self._turn_completed({"turn": candidates[-1]})
        if settled:
            await self._after_turn_end()

    async def _publish_item(
        self, raw: dict[str, Any], completed: bool, timestamp: int | None
    ) -> None:
        item = normalise(raw)
        item_id = str(item.get("id") or "")
        if item_type(item) == "userMessage" and not await self._accept_user_message(item):
            return
        for emit in self._translator.item(raw, completed, timestamp):
            await self._apply(emit)
        if completed and item_id:
            self._last_item_id = item_id

    async def _accept_user_message(self, item: dict[str, Any]) -> bool:
        """Decide whether a `userMessage` item is news, and who typed it.

        The daemon echoes the prompt that started a turn on both `item/started`
        and `item/completed`, so recognising it once is not enough: the item id
        of an echo we matched stays known, and every later sighting of it — the
        second event, a backfill, a reconnect — is dropped. What is left after
        that is somebody else's message, and a `clientId` that is not one the
        daemon has stamped on an echo of ours names the terminal typing it,
        which is the only signal the daemon offers that a TUI is there.
        """
        item_id = str(item.get("id") or "")
        if self._echoes.recognised(item_id):
            return False
        client_id = str(item.get("clientId") or "")
        claimed = self._echoes.claim(item_id, text_of(item.get("content")), client_id)
        if claimed is not None:
            # A steered message is published here rather than where it was sent,
            # so it lands after the output Codex produced before reading it.
            await self._publish_steer(claimed)
            return False
        if client_id and not self._echoes.owns(client_id):
            changed = not self._terminal_holds
            self.claim_terminal()
            if changed:
                await self._control_changed()
        return True

    async def _publish_steer(self, echo: Echo) -> None:
        """Publish the `user_message` a steer has been holding back."""
        fields = echo.bubble()
        if fields is None:
            return
        if self.channel.session.control == "shared":
            fields["delivery"] = "delivered"
        await self.channel.emit("user_message", **fields)

    async def _publish_unread(self, stop_reason: str) -> None:
        """Show the steered messages this turn ended without ever reading.

        Amendment A14 puts a steered bubble where the agent took the message,
        which is nowhere at all when the turn stops first, so the turn's end is
        the last place it can be shown — and an interrupted turn says so.
        """
        for echo in self._echoes.unclaimed():
            await self._publish_steer(echo)
            if stop_reason == "interrupted":
                await self.channel.notice(
                    "warn", "the agent was stopped before it read your message"
                )

    async def _apply(self, emit: Any) -> None:
        if emit.delta:
            fields = dict(emit.fields)
            block_id = str(fields.pop("block_id"))
            delta = str(fields.pop("delta", ""))
            await self.channel.emit_delta(emit.kind, block_id, delta, **fields)
            return
        if emit.kind == "todos":
            await self.channel.publish_todos(list(emit.fields.get("items") or []))
            return
        if emit.kind == "turn_completed":
            return
        if emit.kind == "tool_call" and emit.fields.get("status") == "running":
            block_id = str(emit.fields.get("block_id") or "")
            now = time.monotonic()
            last = self._last_output_flush.get(block_id, 0.0)
            if emit.fields.get("output") and now - last < OUTPUT_THROTTLE:
                return
            self._last_output_flush[block_id] = now
        await self.channel.emit(emit.kind, **emit.fields)

    # --------------------------------------------------------------- driving

    def _inputs(self, text: str, written: list[Attachment]) -> list[dict[str, Any]]:
        """Images travel as inputs; anything else is named in the prompt."""
        images = [item for item in written if item.mime.startswith("image/")]
        others = [item for item in written if not item.mime.startswith("image/")]
        prompt = describe(text, others)
        inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        inputs.extend({"type": "localImage", "path": item.path} for item in images)
        return inputs

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        """Publish the message, then start the turn that answers it."""
        written = materialise(self.channel.session.session_id, attachments) if attachments else []
        await self._deliver(text, self._inputs(text, written), written, source, block_id)

    async def run_prompt(self, prompt: str, shown: str, block_id: str) -> None:
        """Start a turn on a prompt the user never typed, such as `/init` (A27).

        The bubble keeps the command; the daemon's own echo of the prompt is
        recognised by `_deliver` and never becomes a second message.
        """
        await self._deliver(shown, [{"type": "text", "text": prompt}], [], "remote", block_id)

    async def _deliver(
        self,
        shown: str,
        inputs: list[dict[str, Any]],
        written: list[Attachment],
        source: str,
        block_id: str | None,
    ) -> None:
        thread_id = self._thread_id
        if thread_id is None:
            raise RcError("agent_unavailable", "the codex thread is not connected")
        fields: dict[str, Any] = {
            "block_id": block_id or f"user:{uuid.uuid4()}",
            "text": shown,
            "source": source,
        }
        if written:
            fields["attachments"] = wire_attachments(written)
        if self.channel.session.control == "shared":
            fields["delivery"] = "delivered"
        # The message goes out before any request does: everything below is a
        # round trip to the daemon, and the apps have drawn this bubble already.
        await self.channel.emit("user_message", **fields)
        subscribed = await self.try_resume()
        evicted = self._echoes.remember(Echo(str(inputs[0]["text"])))
        if evicted is not None:
            await self._publish_steer(evicted)
        self._local_turn = True
        self._trigger = source
        self._turn_started_at = now_ms()
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": inputs,
            "approvalPolicy": self._permission_mode,
        }
        if self._model:
            params["model"] = self._model
        effort = self._catalog.clamp_effort(self._model, self._effort)
        if effort:
            params["effort"] = effort
        epoch = self._turn_epoch
        self._activity_epoch += 1
        self._start_requests += 1
        self._turn_done.clear()
        try:
            result = await self._client.request("turn/start", params)
        except RcError:
            self._local_turn = False
            if self._turn_id is None and self._held is None:
                self._turn_done.set()
            raise
        finally:
            self._start_requests -= 1
        if not subscribed:
            # The rollout exists once a turn has started, so this is the first
            # moment a thread created in the terminal can be subscribed to.
            # When the resume above worked there is nothing left to do here.
            self.resume_again()
            await self.try_resume()
        turn = result.get("turn") or {}
        async with self._notification_lock:
            if epoch == self._turn_epoch:
                await self._turn_started({"turn": {"id": str(turn.get("id") or uuid.uuid4())}})
        # A thread with no terminal is `remote` while this turn runs.
        await self._control_changed()

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        return await self.steer_with_attachments(text, [], block_id)

    async def steer_with_attachments(
        self, text: str, attachments: list[dict[str, Any]], block_id: str | None = None
    ) -> bool:
        """Steer the running turn with the message as `send` would start one.

        `turn/steer` takes the inputs `turn/start` takes, so images travel as
        inputs and other files are named in the prompt here too. Files Codex
        never got, because it refused the steer, are taken back: the hub
        queues the message instead, and its files are written again then.
        """
        thread_id = self._thread_id
        turn_id = self._turn_id
        if thread_id is None or turn_id is None or turn_id.startswith("unknown:"):
            return False
        written = materialise(self.channel.session.session_id, attachments) if attachments else []
        inputs = self._inputs(text, written)
        try:
            await self._client.request(
                "turn/steer",
                {"threadId": thread_id, "expectedTurnId": turn_id, "input": inputs},
            )
        except RcError:
            discard(written)
            return False
        # Amendment A14: the bubble waits for Codex's echo of this prompt, which
        # arrives at the step that reads it. The apps hold their optimistic row
        # under the same block id until then.
        echo = Echo(
            str(inputs[0]["text"]),
            block_id or f"user:{uuid.uuid4()}",
            shown=text,
            attachments=tuple(wire_attachments(written)),
        )
        evicted = self._echoes.remember(echo)
        if evicted is not None:
            await self._publish_steer(evicted)
        return True

    async def interrupt(self) -> bool:
        thread_id = self._thread_id
        turn_id = self._turn_id
        if thread_id is None:
            return False
        if turn_id is None:
            return await self._interrupt_held()
        self._interrupting = True
        await self.channel.set_state("running", "interrupting")
        self._dialogs.deny_all()
        if not turn_id.startswith("unknown:"):
            try:
                await self._client.request(
                    "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                )
            except RcError:
                log.warning("codex interrupt was rejected; waiting for the turn to settle")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._turn_done.wait(), timeout=DRAIN_TIMEOUT)
        return True

    async def _interrupt_held(self) -> bool:
        """Stop a turn its subagents were holding open.

        The parent's own turn is already over, so there is nothing on this
        thread for `turn/interrupt` to stop; the subagents run in threads of
        their own that the person never sees. Ending the turn is what the stop
        button means here, and the children are let go with it.
        """
        async with self._notification_lock:
            held = self._held
            if held is None:
                return False
            await self.channel.set_state("running", "interrupting")
            self._dialogs.deny_all()
            await self._settle_turn("interrupted", held.usage, held.limit, self._elapsed())
        await self._after_turn_end()
        return True

    # -------------------------------------------------------------- commands

    def facts(self) -> ThreadFacts:
        """What `/status` reports, all of it already known to this session."""
        return ThreadFacts(
            cwd=self._cwd,
            model=self._model,
            effort=self._effort,
            speed=self._speed,
            permission_mode=self._permission_mode,
            sandbox=self._sandbox,
            usage=dict(self._translator.usage),
        )

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._client.request(method, params)

    async def commands(self) -> list[Command]:
        return slash.listing()

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        await slash.run(self, name, argument, block_id)

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        thread_id = self._thread_id
        params: dict[str, Any] = {"threadId": thread_id} if thread_id else {}
        if model is not None:
            self._model = model
            params["model"] = model
        if permission_mode is not None:
            self._permission_mode = permission_mode
            params["approvalPolicy"] = permission_mode
        if effort is not None:
            self._effort = effort
            clamped = self._catalog.clamp_effort(self._model, effort)
            if clamped:
                params["effort"] = clamped
        if speed is not UNSET:
            # Null is what takes the thread back to the standard tier; the
            # daemon then reports the tier as `default`, which reads as null.
            self._speed = speed
            params["serviceTier"] = speed
        if thread_id is None or len(params) < 2:
            return
        await self._client.request("thread/settings/update", params)

    # ------------------------------------------------------- prompts to user

    async def server_request(self, request_id: Any, method: str, params: dict[str, Any]) -> Any:
        return await self._dialogs.request(request_id, method, params)

    async def resolved_elsewhere(self, request_id: Any) -> None:
        await self._dialogs.resolved_elsewhere(request_id)

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        return await self._dialogs.approve(request_id, option_id, message)

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        return await self._dialogs.answer(request_id, answers)
