"""The session hub: create, drive, queue, mirror and tear down sessions."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .. import attachments, procscan
from ..agents.base import AttachmentSteering, SessionRunner
from ..agents.claude import live, transcripts
from ..agents.claude.holders import Holder
from ..agents.codex.daemon.service import CodexDaemonService
from ..agents.codex.models import catalog_cache
from ..agents.grok.service import GrokLeaderService
from ..agents.pi.adapter import PiRunner
from ..agents.pi.service import PiExtensionService
from ..agents.registry import RunnerSpec, offline_commands, runner_for
from ..errors import RcError, TerminalNotReady
from ..git import create_worktree, session_git, slugify
from ..logging_setup import logger
from ..models import (
    UNSET,
    AgentInfo,
    Choice,
    Session,
    SpeedSetting,
    title_from_message,
    title_from_text,
)
from ..registry import Registry
from . import offsets, rename, titles
from .attach import Attachment, HookQuestion, SessionStart, TurnHook
from .bindings import Binding, TerminalBindings
from .channel import SessionChannel
from .limits import LimitStop
from .ptys import PtyLinks
from .queue import hold, queue_ts_from, release, snapshot_entry
from .resume import ResumeScheduler
from .shared import EXIT_SETTLE, SharedControl, SharedState

log = logger("rc_client.hub")

Publisher = Callable[[dict[str, Any]], Awaitable[None]]
MAX_TEXT_BYTES = 64 * 1024
MAX_ATTACHMENTS = 8
# How many removals may wait for a link: a link that never comes back would
# otherwise grow them without end.
MAX_PENDING_REMOVALS = 64
# How long closing a session may take before the reply goes out anyway (A39).
# An agent that will not die is still let go of here: the person asked for the
# session to be closed, and a request that never answers helps nobody.
CLOSE_TIMEOUT = 8.0
# How long a send waits for the bridge of a live terminal CLI that holds its session, and how often
# it looks. A bridge that lost the daemon dials back within its own backoff, five seconds at most
# (`channel/link.py`).
BRIDGE_WAIT = 5.0
BRIDGE_POLL = 0.05
BRIDGE_PENDING = "the terminal for this session has not reconnected yet; try again in a moment"
SCAN_FAILED = "could not tell whether a terminal holds this session; try again in a moment"


def _as_int(value: Any, field: str) -> int | None:
    """Coerce an app-supplied number, reporting a protocol error when it is not one."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RcError("bad_request", f"{field} must be an integer") from exc


class Steerable(Protocol):
    """What a running turn is steered through: a runner, or an attachment (A14, A51)."""

    @property
    def supports_steer(self) -> bool: ...

    async def steer(self, text: str, block_id: str | None = None) -> bool: ...


@dataclass(slots=True)
class SessionEntry:
    session: Session
    channel: SessionChannel
    runner: SessionRunner | None = None
    queue: list[dict[str, Any]] = field(default_factory=list)
    holder_pid: int | None = None
    holder_identity: tuple[int, str] | None = None
    transcript: str | None = None
    shared: SharedState | None = None
    # The messages an attachment injected, by their channel id, until the
    # transcript says what became of them; a bridge registering again keeps
    # them (A51).
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Lets one `session.send` or `session.command` at a time through to the session, in the order
    # they arrived. It is taken before the lock, and held across the wait for a bridge that the
    # lock must stay free for.
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The one task handing the queue its next message, and whether it was
    # asked again while it ran (A43).
    drainer: asyncio.Task[None] | None = None
    drain_again: bool = False


class SessionHub:
    def __init__(
        self,
        registry: Registry,
        publish: Publisher,
        device_id: str,
        agents: Callable[[], list[AgentInfo]],
    ) -> None:
        self.registry = registry
        self.publish = publish
        self.device_id = device_id
        self._agents = agents
        self.entries: dict[str, SessionEntry] = {}
        self.shared = SharedControl(self)
        # The pseudo-terminals the shim started, by CLI pid (A40). The daemon
        # hands over the attach server's own registry; on its own the hub has
        # an empty one, so nothing here depends on the socket being up.
        self.ptys = PtyLinks()
        # Amendment A35: what the device does about a session the usage limit
        # stopped. Replaceable so a test can drive it from its own clock.
        self.resumes = ResumeScheduler(self)
        # Set by the daemon once the shared Codex app-server answers a handshake
        # (amendment A11); absent means the per-session spawn path.
        self.codex_daemon: CodexDaemonService | None = None
        # Set by the daemon when the pi extension socket is listening (A26).
        self.pi_extensions: PiExtensionService | None = None
        # Set by the daemon so Grok sessions can run on the machine's shared
        # leader when the person's configuration asks for it (A28).
        self.grok_leader: GrokLeaderService | None = None
        # The session each terminal CLI's hook last put it in, by its own pid. It outlives the
        # daemon, so a restart does not send an attachment back to the session its CLI started in.
        self.bindings = TerminalBindings(registry)
        # When a CLI started, which tells it from a later process given its pid. Replaceable so
        # a test can name a process that is not there.
        self.process_start = procscan.process_start
        # Removals the device made on its own initiative, and the link each
        # went out on. A frame published while the link is down is dropped, so
        # each one is said again on the next link.
        self._removed: list[tuple[int, str]] = []
        self._link = 0
        # Whether the gateway link is up right now; the daemon wires the real
        # answer in, so a repeated removal is never published into a gap.
        self.link_up: Callable[[], bool] = lambda: True
        # The requests being served, by session and request id, and what they will answer.
        self._flying: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        # Set when the daemon stops: no queue is drained after that.
        self._closed = False
        # The sessions that had messages in their Up next when the daemon last stopped, until the
        # gateway has been told that the queue is empty (`clear_stale_queues`).
        self._stale_queues: list[str] = []
        # The sessions whose queue changed while the link was down, until the gateway has been
        # told what it holds now (`republish_queues`).
        self._unsaid_queues: set[str] = set()

    # ------------------------------------------------------------- accessors

    def agent_info(self, agent: str) -> AgentInfo:
        for info in self._agents():
            if info.agent == agent:
                return info
        raise RcError("agent_unavailable", f"unknown agent {agent}")

    def snapshot(self) -> list[dict[str, Any]]:
        """The sessions a `hello` announces, and the mark that a link is starting."""
        self._link += 1
        return [entry.session.to_dict() for entry in self.entries.values()]

    def entry(self, session_id: str) -> SessionEntry:
        entry = self.entries.get(session_id)
        if entry is None:
            raise RcError("not_found", f"unknown session {session_id}")
        return entry

    def _channel(self, session: Session) -> SessionChannel:
        """A channel wired to the hub, so every turn's end reaches the scheduler."""
        channel = SessionChannel(self.registry, session, self.publish)
        channel.on_turn_end = self._turn_ended
        channel.on_queue_change = self._queue_changed
        return channel

    def _queue_changed(self, session: Session) -> None:
        """A queue changed. With the link down the frame that says so is dropped, and the gateway
        goes on serving the snapshot it had, so the session is said again once there is a link."""
        if not self.link_up():
            self._unsaid_queues.add(session.session_id)

    async def _turn_ended(self, session: Session, limit: LimitStop | None) -> None:
        """Amendment A35: a turn the usage limit ended schedules its own resume."""
        await self.resumes.on_turn_end(session, limit)

    def load(self) -> None:
        """Restore persisted sessions as resumable, terminal-free records.

        `resume` is not restored here: a pending one belongs to the scheduler,
        which reads its own records back and puts them on the sessions (A35).
        """
        self.bindings.load()
        for session in self.registry.load_sessions():
            if not session.session_id or not session.agent:
                continue
            session.device_id = self.device_id
            session.control = "none"
            session.state = "stopped" if session.archived else "idle"
            session.turn = None
            if session.queued:
                self._stale_queues.append(session.session_id)
            session.queued = 0
            session.resume = None
            self.entries[session.session_id] = SessionEntry(
                session=session,
                channel=self._channel(session),
            )

    async def clear_stale_queues(self) -> None:
        """Tell the gateway that the Up next of every session that had one is empty now.

        The queue lived in the memory of the process that stopped, and `load` restored the count
        as zero, but a gateway that did not restart still serves the snapshot it was last told:
        entries that answer `not_found` when an app removes them. It is said once, when the link is
        up, because a frame published while it is down is dropped. The summary follows the event,
        as it does for any change to a queue, and does not move the session up the list: nothing
        here is activity.
        """
        if not self._stale_queues or not self.link_up():
            return
        said = 0
        for session_id in list(self._stale_queues):
            entry = self.entries.get(session_id)
            if entry is not None:
                await entry.channel.emit("queue", pending=[])
                await entry.channel.publish_summary(touch=False)
                said += 1
            self._stale_queues.remove(session_id)
        log.info("told the gateway that the queues of the last run are empty", sessions=said)

    async def republish_queues(self) -> None:
        """Say again what is held in the Up next of every session, for a link that has just come up.

        The gateway keeps only the newest `queue` snapshot of a session, in its memory, and history
        carries none, so a gateway that restarted while this daemon went on has lost every one:
        apps that subscribe would see no Up next until the queue next changed. A queue that
        emptied while the link was down is said too, empty, since the frame that said so was
        dropped and the gateway would go on serving the snapshot it had. The event is the one a
        change publishes, and the summary follows it as it does for any change; neither moves the
        session up the list, since saying it again is not activity.
        """
        for session_id, entry in list(self.entries.items()):
            if (entry.queue or session_id in self._unsaid_queues) and (
                self.entries.get(session_id) is entry
            ):
                await entry.channel.emit("queue", pending=self.queue_snapshot(entry))
                await entry.channel.publish_summary(touch=False)
            self._unsaid_queues.discard(session_id)
        # A session that was removed has nothing left to say.
        self._unsaid_queues.intersection_update(self.entries)

    def register_mirrored(self, session: Session, transcript: str | None = None) -> SessionEntry:
        """Adopt a session discovered from an agent transcript on disk."""
        entry = self.entries.get(session.session_id)
        if entry is not None:
            entry.transcript = transcript or entry.transcript
            return entry
        session.device_id = self.device_id
        entry = SessionEntry(
            session=session,
            channel=self._channel(session),
            transcript=transcript,
        )
        self.entries[session.session_id] = entry
        self.registry.upsert_session(session)
        return entry

    def begin_close(self) -> None:
        """The daemon is stopping. Its bridges close with it, and their CLIs do not.

        From here no queue is drained, and a bridge that goes is not the CLI exiting: what the
        apps are showing of the session stays, for the process that starts next to derive again,
        instead of recording a stop that never happened.
        """
        self._closed = True

    async def close(self) -> None:
        self._closed = True
        for entry in list(self.entries.values()):
            await self._cancel_drain(entry)
            # The queue lives in memory, so its messages end with the daemon,
            # and so do the ones steered into a running turn (A51).
            for item in entry.queue:
                await release(item)
            if entry.shared is not None:
                for item in entry.shared.steers:
                    await release(item)
                entry.shared.attachment.detach()
                entry.shared = None
            if entry.runner is not None:
                with contextlib.suppress(Exception):
                    await entry.runner.close()
                self._mark_released(entry)
            await entry.channel.close()

    # ---------------------------------------------------------------- create

    def _check_choices(self, info: AgentInfo, params: dict[str, Any]) -> None:
        """Reject settings outside what this device advertises in `AgentInfo`.

        `permission_mode` and `effort` are closed enumerations (PROTOCOL.md section 8), and an
        unknown value would only surface later as a CLI that refuses to start. `model` stays open:
        model ids are the agents' own and the detected catalogue can lag a new release. An agent
        that advertises no values for a setting has no such setting, so any value for it is
        `unsupported` (A25, PROTOCOL.md 4.2).
        """
        for field_name, allowed in (
            ("permission_mode", info.permission_modes),
            ("effort", info.efforts),
        ):
            value = params.get(field_name)
            if value is None:
                continue
            if not allowed:
                raise RcError("unsupported", f"{info.agent} has no {field_name} setting")
            if value not in {choice.id for choice in allowed}:
                raise RcError("bad_request", f"unknown {field_name} for {info.agent}: {value}")

    async def _speeds_for(self, info: AgentInfo, model: str | None) -> list[Choice]:
        """The tiers one model offers, which `AgentInfo` only carries the union of."""
        if info.agent != "codex" or not info.path:
            return list(info.speeds)
        catalog = await catalog_cache.get(info.path)
        return catalog.speeds_for(model)

    async def _check_speed(self, info: AgentInfo, speed: SpeedSetting, model: str | None) -> None:
        """Reject a tier this agent, or this model, cannot run at (amendment A21).

        A request that never mentioned the key asks for nothing; one that sent
        null asks for the standard speed, which an agent with no tiers refuses
        along with every other value.
        """
        if speed is UNSET:
            return
        if not info.speeds:
            raise RcError("unsupported", f"{info.agent} has no speed tiers")
        if speed is None:
            return
        allowed = await self._speeds_for(info, model)
        if not allowed:
            raise RcError("unsupported", "this model has no faster tier")
        if speed not in {choice.id for choice in allowed}:
            raise RcError("bad_request", f"unknown speed for {info.agent}: {speed}")

    async def create(self, params: dict[str, Any]) -> dict[str, Any]:
        agent = str(params.get("agent") or "")
        info = self.agent_info(agent)
        if not info.available:
            raise RcError("agent_unavailable", f"{agent} is not installed on this device")
        self._check_choices(info, params)
        model = params.get("model") or info.default_model
        speed: SpeedSetting = params.get("speed") if "speed" in params else UNSET
        await self._check_speed(info, speed, model)
        cwd = str(params.get("cwd") or "")
        if not cwd or not Path(cwd).expanduser().is_dir():
            raise RcError("bad_request", f"working directory does not exist: {cwd}")
        cwd = str(Path(cwd).expanduser().resolve())

        first_message = str(params.get("first_message") or "")
        chosen = title_from_text(str(params.get("title") or ""))
        title = chosen or title_from_message(first_message)
        worktree = bool(params.get("worktree"))
        if worktree:
            cwd = await create_worktree(cwd, slugify(title or agent))

        session = Session(
            session_id=str(uuid.uuid4()),
            device_id=self.device_id,
            agent=agent,
            cwd=cwd,
            title=title,
            state="starting",
            origin="remote",
            control="remote",
            model=model,
            permission_mode=params.get("permission_mode") or info.default_permission_mode,
            effort=params.get("effort") or info.default_effort,
            speed=params.get("speed"),
        )
        session.git = await session_git(cwd, worktree=worktree)
        entry = SessionEntry(session=session, channel=self._channel(session))
        self.entries[session.session_id] = entry
        entry.channel.start()
        self.registry.upsert_session(session)
        if chosen:
            # A title the user typed outranks anything the agent later produces.
            titles.pin(self.registry, session.session_id)

        try:
            entry.runner = await self._build_runner(entry, info, resume=None)
            await entry.runner.start()
        except RcError:
            self.entries.pop(session.session_id, None)
            raise
        except Exception as exc:
            self.entries.pop(session.session_id, None)
            raise RcError("agent_unavailable", f"could not start {agent}: {exc}") from exc

        await entry.channel.set_state("idle")
        if first_message:
            await self._start_turn(entry, first_message, None)
        return {"session": entry.session.to_dict()}

    async def _build_runner(
        self, entry: SessionEntry, info: AgentInfo, resume: str | None
    ) -> SessionRunner:
        async def on_turn_end() -> None:
            self.schedule_drain(entry)

        async def on_session_id(real_id: str) -> None:
            await self.rekey(entry, real_id)

        runner = await runner_for(
            info.agent,
            RunnerSpec(
                entry=entry,
                info=info,
                resume=resume,
                on_turn_end=on_turn_end,
                on_session_id=on_session_id,
                codex_daemon=self.codex_daemon,
                grok_leader=self.grok_leader,
            ),
        )
        if isinstance(runner, PiRunner) and self.pi_extensions is not None:
            # The child's extension dials the socket while `start` is still
            # running, so the service has to know whose link it is by then.
            self.pi_extensions.expect(runner)
        return runner

    async def rekey(self, entry: SessionEntry, real_id: str) -> None:
        old_id = entry.session.session_id
        if old_id == real_id:
            return
        self.registry.rekey_session(old_id, real_id)
        titles.rekey(self.registry, old_id, real_id)
        attachments.rekey(old_id, real_id)
        self.entries.pop(old_id, None)
        entry.session.session_id = real_id
        self.entries[real_id] = entry
        await self.publish({"type": "session.removed", "session_id": old_id})
        await entry.channel.publish_summary()

    # ------------------------------------------------------------------ send

    async def send(self, params: dict[str, Any], source: str = "remote") -> dict[str, Any]:
        """Deliver one message. `source` is the trigger it carries (PROTOCOL 5.2).

        Only the scheduler sends anything but `remote`: its prompt is the one
        message the device writes for the person (A35), and it must not cancel
        the very resume it is running.
        """
        session_id = str(params.get("session_id") or "")
        request_id = str(params.get("id") or "")
        entry = self.entry(session_id)
        cached = self.registry.recall_request(session_id, request_id) if request_id else None
        if cached is not None:
            return cached
        if source == "remote":
            # As it arrives, not when it gets through the gate: a person's message that waits
            # behind another request must still cancel a resume that falls due meanwhile.
            await self.resumes.cancelled_by_person(session_id)
        return await self._in_order(
            entry, request_id, lambda arrived: self._send(entry, params, source, arrived)
        )

    async def _in_order(
        self,
        entry: SessionEntry,
        request_id: str,
        run: Callable[[float], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Serve one request to a session once per id, after the ones that arrived before it.

        A repeat of an id that is still being served, an app retrying after a timeout, gets the
        outcome of the first and is never served itself: serving it again would deliver the
        message twice (PROTOCOL 2.4). The gate lets one request at a time through to the
        session, in the order they came, so a request that waits for something, a bridge that
        is still dialling back, is not overtaken by the one after it. Both are held for the
        whole of a request, which the session's own lock was not. `run` is told when the request
        arrived, so that what it may wait for is counted from then and not from its turn at the
        gate: a queue of requests that all wait for a bridge that is not coming is refused
        together, not one after another.
        """
        arrived = asyncio.get_running_loop().time()
        key = (entry.session.session_id, request_id)
        flying = self._flying.get(key) if request_id else None
        if flying is not None:
            return dict(await asyncio.shield(flying))
        outcome: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        if request_id:
            self._flying[key] = outcome
        try:
            async with entry.gate:
                result = await run(arrived)
        except asyncio.CancelledError:
            outcome.cancel()
            raise
        except BaseException as exc:
            outcome.set_exception(exc)
            outcome.exception()  # nobody may be waiting, and that is not an error to log
            raise
        else:
            outcome.set_result(result)
            return result
        finally:
            if request_id:
                self._flying.pop(key, None)

    async def _send(
        self, entry: SessionEntry, params: dict[str, Any], source: str, arrived: float
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        request_id = str(params.get("id") or "")
        text = str(params.get("text") or "")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise RcError("too_large", "message text exceeds 64 KiB")
        attachments = params.get("attachments") or []
        if len(attachments) > MAX_ATTACHMENTS:
            raise RcError("too_large", "at most 8 attachments per message")
        if attachments:
            self._check_shared_attachments(entry)
        mode = str(params.get("mode") or "auto")
        # A43: an edited message comes back under the `ts` its queue entry had.
        # It never decides whether the message queues; `mode` does.
        queue_ts = queue_ts_from(params)

        # Before the lock: the bridge it waits for registers under it.
        await self._await_terminal(entry, arrived)
        async with entry.lock:
            if entry.shared is not None:
                result = await self._send_shared(
                    entry, mode, request_id, text, attachments, source, queue_ts
                )
                if request_id:
                    self.registry.remember_request(session_id, request_id, result)
                return result
            if entry.session.control == "terminal":
                raise RcError("conflict", self._terminal_conflict_message(entry))
            if entry.runner is None:
                await self._resume(entry)
            runner = entry.runner
            assert runner is not None
            # Amendment A15: the send is going through, so the session is not
            # archived any more even when the message only joins the queue.
            await entry.channel.revive()
            if mode == "auto":
                # A turn's end drains from a task of its own, so a message the
                # turn held may not have gone yet; it goes before this one.
                await self._drain_queue_locked(entry)

            if mode == "queue":
                # An explicit queue request always queues, even when idle: the
                # app told the user their message would wait its turn.
                result = await self._enqueue(entry, request_id, text, attachments, source, queue_ts)
            elif not runner.busy:
                await self._start_turn(entry, text, attachments, request_id, source)
                result = {"accepted": "sent"}
            elif mode == "interrupt":
                await runner.interrupt()
                await self._start_turn(entry, text, attachments, request_id, source)
                result = {"accepted": "sent"}
            elif mode == "auto" and await self._steer(runner, text, attachments, request_id):
                result = {"accepted": "steered"}
            else:
                result = await self._enqueue(entry, request_id, text, attachments, source, queue_ts)
            if mode == "queue" and not runner.busy:
                await self._drain_queue_locked(entry)

        if request_id:
            self.registry.remember_request(session_id, request_id, result)
        return result

    async def _start_turn(
        self,
        entry: SessionEntry,
        text: str,
        attachments: list[dict[str, Any]] | None,
        request_id: str = "",
        source: str = "remote",
    ) -> None:
        """Start a turn for a message an app sent, under that request's own id.

        Amendment A12: the app has already drawn the bubble under the id it
        chose, so the device's `user_message` has to arrive under the same one
        or the message appears twice.
        """
        runner = entry.runner
        if runner is None:
            raise RcError("agent_unavailable", "the session is not running")
        if source != "resume":
            # A session is named after something somebody said, and the resume
            # prompt is the device's own sentence (A35, 7.2).
            await titles.from_prompt(entry.channel, text, attachments)
        await runner.send(text, attachments, source=source, block_id=request_id or None)

    async def _send_shared(
        self,
        entry: SessionEntry,
        mode: str,
        request_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        source: str,
        queue_ts: int | None,
    ) -> dict[str, Any]:
        """A message for a session a terminal CLI shares with the device (A10, A51).

        `auto` steers a turn the CLI is running, when the CLI runs the device's
        turn hooks; every other message, a steer the session cannot take, and
        the resume prompt of A35, which is the device's and never steers, goes
        in now if the CLI is idle and waits its turn otherwise (A19).
        """
        steering = self.shared.steering(entry, source)
        steers = mode == "auto" and source != "resume"
        if steers and await self._steer(steering, text, attachments, request_id):
            return {"accepted": "steered"}
        return await self.shared.send(
            entry, request_id, text, attachments, source, queue_ts, mode=mode
        )

    @staticmethod
    async def _steer(
        target: Steerable, text: str, attachments: list[dict[str, Any]], request_id: str
    ) -> bool:
        """Steer the running turn with the whole message, files included (A14).

        A runner that cannot take files into a running turn is not asked to
        steer a message that has any: it is queued instead, so its files reach
        the agent with its words when the turn ends rather than never.
        """
        if not target.supports_steer:
            return False
        block_id = request_id or None
        if not attachments:
            return await target.steer(text, block_id)
        if isinstance(target, AttachmentSteering):
            return await target.steer_with_attachments(text, attachments, block_id)
        return False

    def _check_shared_attachments(self, entry: SessionEntry) -> None:
        """Refuse files a shared session's attachment cannot hand its agent (4.2, A49)."""
        if self._is_shared(entry) and not self._agent_flag(entry, "shared_attachments"):
            raise RcError("unsupported", "attachments cannot be delivered to a terminal session")

    def _terminal_conflict_message(self, entry: SessionEntry) -> str:
        """Only offer "take over" when this agent can actually be taken over."""
        try:
            info = self.agent_info(entry.session.agent)
        except RcError:
            return "controlled by the terminal on this device"
        if "takeover" in info.capabilities:
            return "controlled by terminal; take over first"
        return "controlled by the terminal on this device; stop it there to continue here"

    async def _enqueue(
        self,
        entry: SessionEntry,
        request_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        source: str = "remote",
        queue_ts: int | None = None,
    ) -> dict[str, Any]:
        """Hold a message until the turn boundary, keeping its attachments.

        The item's id is the app's request id where there is one, and it is
        also the id its `user_message` carries when the queue drains, so the
        bubble the app drew on sending is the one that is filled in later. An
        edited message is held where it was taken out from (A43).
        """
        queued_id = request_id or str(uuid.uuid4())
        item: dict[str, Any] = {
            "id": queued_id,
            "text": text,
            "attachments": attachments,
            "source": source,
        }
        hold(entry.queue, item, queue_ts)
        await entry.channel.publish_queue(self.queue_snapshot(entry))
        return {"accepted": "queued", "queued_id": queued_id}

    def queue_snapshot(self, entry: SessionEntry) -> list[dict[str, Any]]:
        """The wire form of the queue: files stay on the device, only their count goes (A43)."""
        return [snapshot_entry(item) for item in entry.queue]

    def schedule_drain(self, entry: SessionEntry) -> None:
        """Hand the queue its next message from a task of the session's own.

        What ends a turn, and the transcript read that says a terminal's turn
        ended, never wait for a drain: the send holding the session's lock may
        be waiting on that very turn's end (an interrupt), and a settings change
        typed into a terminal holds the lock until the reader confirms it. One
        drain runs per session at a time; asked again meanwhile, it goes round
        once more when it is done (A43).
        """
        if self._closed:
            return
        drainer = entry.drainer
        if drainer is not None and not drainer.done():
            entry.drain_again = True
            return
        entry.drain_again = False
        entry.drainer = asyncio.get_running_loop().create_task(
            self._drain(entry), name="session-drain"
        )

    async def _drain(self, entry: SessionEntry) -> None:
        """The drain task: the oldest held message, under the lock a send takes.

        One the person sends meanwhile waits until the agent is busy with this
        one, so nothing overtakes it while its files are written (A43). A
        failure is reported by type alone; what it says may name a file.
        """
        while True:
            try:
                async with entry.lock:
                    await self._drain_once(entry)
            except Exception as exc:
                log.warning("draining a session's queue failed", error=type(exc).__name__)
            if not entry.drain_again:
                return
            entry.drain_again = False

    async def _drain_once(self, entry: SessionEntry) -> None:
        if self.entries.get(entry.session.session_id) is not entry:
            # The session is gone, and its queue went with it.
            return
        if entry.shared is not None:
            await self.shared.drain_locked(entry)
        else:
            await self._drain_queue_locked(entry)

    @staticmethod
    async def _cancel_drain(entry: SessionEntry) -> None:
        drainer, entry.drainer = entry.drainer, None
        entry.drain_again = False
        if drainer is None or drainer.done() or drainer is asyncio.current_task():
            return
        drainer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drainer

    async def _drain_queue_locked(self, entry: SessionEntry) -> None:
        """Launch the oldest queued message if the agent is idle; the caller holds the lock."""
        item = self._take_queued(entry)
        if item is not None:
            await self._send_queued(entry, item)

    def _take_queued(self, entry: SessionEntry) -> dict[str, Any] | None:
        """The next held message, unless a turn runs or the session is being closed (A39)."""
        if not entry.queue or entry.runner is None or entry.runner.busy:
            return None
        if entry.channel.closing:
            return None
        return entry.queue.pop(0)

    async def _send_queued(self, entry: SessionEntry, item: dict[str, Any]) -> None:
        await entry.channel.publish_queue(self.queue_snapshot(entry))
        runner = entry.runner
        if runner is None:
            return
        try:
            await runner.send(
                str(item["text"]),
                item.get("attachments") or None,
                # A resume that had to wait for a turn is still a resume (A35);
                # anything else went into the queue as the person's message.
                source="resume" if item.get("source") == "resume" else "queue",
                block_id=str(item["id"]),
            )
        except RcError as exc:
            await entry.channel.error(exc.message, code=exc.code)

    async def _resume(self, entry: SessionEntry) -> None:
        """Reconnect a `control: "none"` session before sending to it."""
        info = self.agent_info(entry.session.agent)
        if not info.available:
            raise RcError("agent_unavailable", f"{entry.session.agent} is not installed")
        entry.channel.start()
        runner = await self._build_runner(entry, info, resume=entry.session.session_id)
        try:
            await runner.start()
        except Exception as exc:
            raise RcError("agent_unavailable", f"could not resume the session: {exc}") from exc
        entry.runner = runner
        entry.session.control = "remote"
        await entry.channel.set_state("idle")
        if self.codex_daemon is not None and entry.session.agent == "codex":
            await self.codex_daemon.publish_control(entry)
        if self.grok_leader is not None and entry.session.agent == "grok":
            await self.grok_leader.publish_control(entry)
        await entry.channel.notice("info", "resumed this session from its transcript")

    async def _await_terminal(self, entry: SessionEntry, arrived: float) -> None:
        """Never resume a session a live terminal CLI holds: wait for its bridge, or refuse.

        Resuming starts a second Claude on the transcript the terminal's CLI is writing. A CLI
        the shim started is not attached for a moment after the daemon restarts, until its
        bridge dials back, and a send that was held for the daemon, or came in just after it,
        would find the session held by nobody and do exactly that. So a session a live CLI
        holds waits up to `BRIDGE_WAIT` for the bridge, counted from when the request arrived,
        and the send then goes through it. One whose bridge does not come is refused as
        retryable, and one that never will have a bridge, a CLI the shim did not start, as the
        terminal's, as it always was. A session the scan already shows as the terminal's is not
        looked up: it is refused as it always was.
        """
        session = entry.session
        if session.agent != "claude" or entry.shared is not None or entry.runner is not None:
            return
        if session.control == "terminal":
            # The scan has already found its CLI, and a send to such a session has its own answer.
            return
        holder = await self._terminal_holder(session.session_id)
        if holder is None:
            return
        if not holder.attachable:
            raise RcError("conflict", self._terminal_conflict_message(entry))
        log.info("a send is waiting for the bridge of the terminal that holds its session")
        if not await self._bridge_registers(entry, arrived + BRIDGE_WAIT):
            log.warning("the bridge of a terminal that holds a session did not register in time")
            raise TerminalNotReady(BRIDGE_PENDING)

    async def _terminal_holder(self, session_id: str) -> Holder | None:
        """The live terminal CLI that is in this session, if there is one.

        Claude Code's own record of a CLI, the session its argv resumed and what its hook last
        said name it, the later word of them deciding when they differ (`TerminalBindings.refine`).
        It raises `TerminalNotReady` when the process table could not be read.
        """
        from ..agents.claude.holders import scan_holders

        scan = self.bindings.refine(await scan_holders(cwds=False))
        if not scan.complete:
            # Not being able to read the process table is not the same as nobody holding the
            # session, and resuming on a guess is what this exists to prevent.
            log.warning("the process table could not be read to tell who holds a session")
            raise TerminalNotReady(SCAN_FAILED)
        return next((holder for holder in scan.holders if holder.session_id == session_id), None)

    @staticmethod
    async def _bridge_registers(entry: SessionEntry, deadline: float) -> bool:
        """Whether a bridge attaches to this session by `deadline`, on the event loop's clock."""
        loop = asyncio.get_running_loop()
        while entry.shared is None:
            left = deadline - loop.time()
            if left <= 0:
                return False
            await asyncio.sleep(min(BRIDGE_POLL, left))
        return True

    # -------------------------------------------------------------- controls

    async def stop(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = self.entry(str(params.get("session_id") or ""))
        if self._is_shared(entry):
            if not self._agent_flag(entry, "shared_interrupt"):
                raise RcError("unsupported", "stop it in the terminal")
            if entry.shared is not None and entry.session.agent == "claude":
                # A42: a channel cannot interrupt, so the device presses
                # Escape in the terminal the shim owns instead.
                await self.shared.stop(entry)
                return {}
        if entry.runner is not None:
            await entry.runner.interrupt()
        return {}

    @staticmethod
    def _is_shared(entry: SessionEntry) -> bool:
        """A session a live CLI owns and the device is attached to (A10 4.4)."""
        return entry.shared is not None or entry.session.control == "shared"

    def _agent_flag(self, entry: SessionEntry, name: str) -> bool:
        try:
            return bool(getattr(self.agent_info(entry.session.agent), name))
        except RcError:
            return False

    async def approve(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = self.entry(str(params.get("session_id") or ""))
        if entry.shared is not None:
            message = params.get("message")
            return await self.shared.approve(
                entry,
                str(params.get("request_id") or ""),
                str(params.get("option_id") or ""),
                message if isinstance(message, str) else None,
            )
        if entry.runner is None:
            raise RcError("conflict", "the session is not running")
        ok = await entry.runner.approve(
            str(params.get("request_id") or ""),
            str(params.get("option_id") or ""),
            params.get("message"),
        )
        if not ok:
            raise RcError("not_found", "that approval is no longer pending")
        return {}

    async def answer(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = self.entry(str(params.get("session_id") or ""))
        answers = params.get("answers")
        if not isinstance(answers, dict):
            raise RcError("bad_request", "answers must be an object")
        if entry.shared is not None:
            # Amendment A20: the question the CLI's own dialog is showing.
            return await self.shared.answer(entry, str(params.get("request_id") or ""), answers)
        if entry.runner is None:
            raise RcError("conflict", "the session is not running")
        ok = await entry.runner.answer(str(params.get("request_id") or ""), answers)
        if not ok:
            raise RcError("not_found", "that question is no longer pending")
        return {}

    async def set_options(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = self.entry(str(params.get("session_id") or ""))
        info = self.agent_info(entry.session.agent)
        self._check_choices(info, params)
        model = params.get("model")
        permission_mode = params.get("permission_mode")
        effort = params.get("effort")
        # A55: a name that is refused is refused before anything is applied.
        title = rename.requested(params)
        # A `speed` of null is a session going back to the standard tier, so
        # only the key's absence means "leave it alone" (amendment A21).
        speed: SpeedSetting = params.get("speed") if "speed" in params else UNSET
        await self._check_speed(info, speed, model or entry.session.model)
        if self._is_shared(entry):
            await self._set_shared(entry, info, model, permission_mode, effort, speed)
            if entry.session.agent == "claude":
                # A40: the typist published what the transcript says the CLI
                # took, which for a capped effort is lower than the level asked.
                model = effort = None
        if entry.runner is not None:
            await entry.runner.apply_settings(model, permission_mode, effort, speed)
        meta: dict[str, Any] = {} if speed is UNSET else {"speed": speed}
        for key, value in (
            ("model", model),
            ("permission_mode", permission_mode),
            ("effort", effort),
        ):
            if value is not None:
                meta[key] = value
        await entry.channel.set_meta(**meta)
        if title is not None:
            # A55: the name goes last. It is the agent's own, so it is asked for
            # there, and the reply carries the name the agent now holds.
            await rename.apply(self, entry, title)
        return {"session": entry.session.to_dict()}

    async def _set_shared(
        self,
        entry: SessionEntry,
        info: AgentInfo,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting,
    ) -> None:
        """Apply what the attachment can, and refuse what the terminal keeps.

        `shared_settings_keys` names the settings an agent's attachment can
        change; absent, it can change all four (A40, 4.2). What it cannot is
        still the terminal's, which is what an app draws as a value (A17).
        """
        asked = {
            "model": model,
            "permission_mode": permission_mode,
            "effort": effort,
            "speed": None if speed is UNSET else speed,
        }
        wanted = {key for key, value in asked.items() if value is not None}
        if speed is not UNSET:
            wanted.add("speed")
        if not wanted:
            return
        allowed = set(info.shared_settings_keys or asked) if info.shared_settings else set()
        if wanted - allowed:
            raise RcError("unsupported", "change it in the terminal")
        if entry.session.agent == "claude":
            # A40: the device types the change into the terminal and answers
            # only once the transcript says the CLI took it.
            await self.shared.set_settings(entry, model, effort)

    # ------------------------------------------------------------- commands

    def _check_commands_capability(self, entry: SessionEntry) -> None:
        info = self.agent_info(entry.session.agent)
        if "commands" not in info.capabilities:
            raise RcError("unsupported", f"{entry.session.agent} takes no commands from here")

    async def commands(self, params: dict[str, Any]) -> dict[str, Any]:
        """What the session offers now (A27): the live process's list, or what is
        known without starting one."""
        entry = self.entry(str(params.get("session_id") or ""))
        self._check_commands_capability(entry)
        if entry.runner is not None:
            listed = await entry.runner.commands()
        else:
            listed = await offline_commands(entry.session.agent, entry.session)
        return {"commands": [command.to_dict() for command in listed]}

    async def command(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run one slash command (A27). Same gates as `send`, plus "idle only"."""
        session_id = str(params.get("session_id") or "")
        request_id = str(params.get("id") or "")
        entry = self.entry(session_id)
        cached = self.registry.recall_request(session_id, request_id) if request_id else None
        if cached is not None:
            return cached
        self._check_commands_capability(entry)
        await self.resumes.cancelled_by_person(session_id)
        return await self._in_order(
            entry, request_id, lambda arrived: self._command(entry, params, arrived)
        )

    async def _command(
        self, entry: SessionEntry, params: dict[str, Any], arrived: float
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        request_id = str(params.get("id") or "")
        name = str(params.get("name") or "").strip().lstrip("/")
        if not name:
            raise RcError("bad_request", "name is required")
        argument = str(params.get("argument") or "").strip() or None
        await self._await_terminal(entry, arrived)
        if entry.shared is not None:
            # A40: a Claude channel carries no commands, so the device types
            # this one into the terminal instead, under the same lock.
            if argument:
                raise RcError("bad_request", f"/{name} takes no argument")
            await self.shared.run_command(entry, name, request_id or str(uuid.uuid4()))
            typed: dict[str, Any] = {}
            if request_id:
                self.registry.remember_request(session_id, request_id, typed)
            return typed
        async with entry.lock:
            if entry.session.control == "terminal":
                raise RcError("conflict", self._terminal_conflict_message(entry))
            if entry.runner is None:
                await self._resume(entry)
            runner = entry.runner
            assert runner is not None
            if runner.busy:
                raise RcError("conflict", "wait for the turn to finish")
            await entry.channel.revive()
            await runner.command(name, argument, request_id or str(uuid.uuid4()))
        result: dict[str, Any] = {}
        if request_id:
            self.registry.remember_request(session_id, request_id, result)
        return result

    async def history(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        self.entry(session_id)
        before_seq = _as_int(params.get("before_seq"), "before_seq")
        after_seq = _as_int(params.get("after_seq"), "after_seq")
        if before_seq is not None and after_seq is not None:
            raise RcError("bad_request", "before_seq and after_seq are mutually exclusive")
        limit = _as_int(params.get("limit"), "limit") or 200
        events, has_more = self.registry.history(session_id, before_seq, limit, after_seq)
        return {"events": events, "has_more": has_more}

    async def block(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        self.entry(session_id)
        event = self.registry.block(session_id, str(params.get("block_id") or ""))
        if event is None:
            raise RcError("not_found", "unknown block")
        return {"event": event}

    async def queue_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        entry = self.entry(str(params.get("session_id") or ""))
        queued_id = str(params.get("queued_id") or "")
        removed = [item for item in entry.queue if item["id"] == queued_id]
        if not removed:
            raise RcError("not_found", "that message is not queued")
        entry.queue = [item for item in entry.queue if item["id"] != queued_id]
        for item in removed:
            await release(item)
        await entry.channel.publish_queue(self.queue_snapshot(entry))
        return {}

    async def archive(self, params: dict[str, Any]) -> dict[str, Any]:
        """Close a session the device drives, then archive it (amendment A39).

        `archived: false` only clears the flag; the session comes back to life
        through the paths A15 names.
        """
        entry = self.entry(str(params.get("session_id") or ""))
        archived = bool(params.get("archived"))
        if archived and entry.runner is not None:
            await self._close_session(entry)
            return {"session": entry.session.to_dict()}
        entry.session.archived = archived
        await entry.channel.publish_summary()
        return {"session": entry.session.to_dict()}

    async def _close_session(self, entry: SessionEntry) -> None:
        """End what the device holds for the agent, then say so once (A39).

        The session is marked as being closed before anything is asked of the
        agent, so its last words — a turn ending, a status going idle — cannot
        take it back out of the Archive, and the end of the interrupted turn
        drains nothing: the messages held for the session are discarded with
        it, files and all, rather than started as the turn the close is there
        to end. Everything else is one publish at the end: the row goes from
        working to closed and archived in a single step, whether or not the
        agent went quietly.
        """
        runner = entry.runner
        entry.channel.closing = True
        try:
            # A drain under way would hand its message to an agent the close is
            # about to end, and say so on the archived session. It is stopped,
            # and the files it was writing are taken back (`attachments.store`).
            await self._cancel_drain(entry)
            await asyncio.wait_for(self._shutdown(runner), CLOSE_TIMEOUT)
        except TimeoutError:
            log.warning("a session did not close in time; letting go of it anyway")
        except Exception as exc:
            log.warning("closing a session failed", error=str(exc)[:200])
        finally:
            entry.runner = None
            entry.session.archived = True
            entry.session.control = "none"
            entry.session.state = "stopped"
            entry.session.state_detail = None
            entry.session.turn = None
            held, entry.queue = entry.queue, []
            entry.channel.closing = False
        self._mark_released(entry)
        for item in held:
            await release(item)
        if held:
            await entry.channel.publish_queue(self.queue_snapshot(entry))
        await entry.channel.publish_summary()

    @staticmethod
    def _mark_released(entry: SessionEntry) -> None:
        """A terminal session the device has let go of is read on from here.

        Its runner published everything the transcript holds so far; the
        mirror that takes the session back must not publish it again.
        """
        if entry.session.origin != "remote":
            offsets.mark_end(entry.channel.registry, entry.session.session_id, entry.transcript)

    @staticmethod
    async def _shutdown(runner: SessionRunner | None) -> None:
        """Stop the work, then end the session for good."""
        if runner is None:
            return
        if runner.busy:
            with contextlib.suppress(Exception):
                await runner.interrupt()
        await runner.shutdown()

    async def delete(self, params: dict[str, Any]) -> dict[str, Any]:
        await self._remove(self.entry(str(params.get("session_id") or "")))
        return {}

    async def _remove(self, entry: SessionEntry) -> None:
        """Forget a session here and everywhere: nothing of it is kept."""
        session_id = entry.session.session_id
        await self._cancel_drain(entry)
        await self.shared.forget(entry)
        if entry.runner is not None:
            await entry.runner.close()
        await entry.channel.close()
        self.entries.pop(session_id, None)
        self.registry.delete_session(session_id)
        await asyncio.to_thread(attachments.cleanup, session_id)
        await self.publish({"type": "session.removed", "session_id": session_id})

    async def takeover(self, params: dict[str, Any]) -> dict[str, Any]:
        from ..agents.claude.holders import release_holder

        entry = self.entry(str(params.get("session_id") or ""))
        if self._is_shared(entry):
            raise RcError("conflict", "already attached")
        info = self.agent_info(entry.session.agent)
        if "takeover" not in info.capabilities:
            raise RcError("unsupported", f"{entry.session.agent} sessions cannot be taken over")
        if entry.session.control != "terminal":
            raise RcError("conflict", "this session is not controlled by a terminal")
        if entry.session.state == "running":
            raise RcError("conflict", "the terminal session is busy; try again when it is idle")
        if entry.holder_pid is None or entry.holder_identity is None:
            raise RcError("conflict", "cannot identify the terminal process holding this session")
        released = await release_holder(entry.holder_pid, entry.holder_identity)
        if not released:
            raise RcError("conflict", "the terminal process did not release the session")
        entry.holder_pid = None
        entry.holder_identity = None
        entry.session.control = "none"
        await entry.channel.notice("info", "took over from the terminal")
        await self._resume(entry)
        return {"session": entry.session.to_dict()}

    # ------------------------------------------------------------ attachment

    async def attach_registered(self, attachment: Attachment) -> None:
        """A channel bridge claimed a terminal session (A10: `terminal → shared`)."""
        entry = await self._attach_entry(attachment)
        # What the hook map said when the bridge was placed: nothing awaited since.
        heard = self.bindings.get(attachment.pid)
        if entry is None:
            attachment.detach()
            return
        async with entry.lock:
            await self.shared.registered(entry, attachment)
        await self._follow_hook(attachment, heard)

    async def _follow_hook(self, attachment: Attachment, heard: Binding | None) -> None:
        """Move a bridge that a hook, heard while it registered, has put in another session.

        A hook cannot move a bridge that is not attached yet, and one that arrives between the
        moment the bridge is placed and the moment it is attached would be lost.
        """
        current = self.bindings.get(attachment.pid)
        if current is None or current is heard:
            return
        if current.start.session_id == attachment.session_id:
            return
        entry = self._attached_to(attachment.pid)
        if entry is not None:
            await self._move_attachment(entry, current.start)

    async def _attach_entry(self, attachment: Attachment) -> SessionEntry | None:
        """The session this bridge speaks for, which is not always the one it names.

        Claude Code hands an MCP server the environment it was spawned with, so
        a bridge reports the id the CLI chose at startup for as long as it runs.
        Where its CLI's hook has said otherwise, now or before the daemon
        restarted, the hook is the fresher account and the attachment follows
        it; so does Claude Code's own record of the CLI where the hook said
        nothing.
        """
        start = await self._bound_start(attachment)
        if start is not None and start.session_id != attachment.session_id:
            log.info("a bridge was put in the session its CLI is in", source=start.source)
            attachment.session_id = start.session_id
            return self._terminal_entry(start.session_id, attachment.cwd, start.transcript_path)
        return self._terminal_entry(attachment.session_id, attachment.cwd, "")

    async def _bound_start(self, attachment: Attachment) -> SessionStart | None:
        """Where the CLI behind this bridge is: its hook's last word or Claude Code's record.

        Both are checked against the process, because a pid that a later process was given is not
        the CLI that had it. A process whose start time cannot be read cannot be told from its
        predecessor, so the hook's word stands and the record's is not believed. Where the two
        disagree the later one wins: the CLI may have moved since the hook spoke, with the daemon
        down to miss the next one, or the record may be the older account.
        """
        pid = attachment.pid
        if pid <= 0:
            return None
        process = await self.process_start(pid)
        records = await asyncio.to_thread(live.read_records)
        # After everything awaited, so a hook heard while the file was being read is not missed.
        binding = self.bindings.get(pid)
        if binding is not None and process is not None and not binding.is_process(pid, process):
            self.bindings.forget(pid)
            binding = None
        record = live.verified(records, pid, process)
        if record is not None and (
            binding is None
            or (record.session_id != binding.start.session_id and record.updated_at > binding.at)
        ):
            return SessionStart(
                session_id=record.session_id,
                cwd=attachment.cwd,
                pid=pid,
                source="startup",
                transcript_path="",
            )
        return binding.start if binding is not None else None

    def _terminal_entry(self, session_id: str, cwd: str, transcript: str) -> SessionEntry | None:
        """The entry a terminal CLI attaches to, created the first time it is seen."""
        entry = self.entries.get(session_id)
        if entry is not None:
            if entry.runner is not None:
                # This device already drives the session; a second writer would
                # interleave two conversations in one transcript.
                log.warning("refusing a channel attachment for a session we drive")
                return None
            entry.transcript = entry.transcript or transcript or None
            return entry
        session = Session(
            session_id=session_id,
            device_id=self.device_id,
            agent="claude",
            cwd=cwd,
            title="",
            state="idle",
            origin="terminal",
            control="shared",
        )
        entry = self.register_mirrored(session, transcript or None)
        entry.channel.start()
        return entry

    async def attach_permission_request(
        self, attachment: Attachment, payload: dict[str, Any]
    ) -> None:
        entry = self.entries.get(attachment.session_id)
        if entry is None:
            return
        async with entry.lock:
            await self.shared.permission_request(entry, payload)

    async def attach_question(self, question: HookQuestion) -> None:
        """A `PermissionRequest` hook: the CLI is asking its person something (A20)."""
        entry = self.entries.get(question.session_id)
        if entry is None or entry.shared is None:
            # Nothing is attached to this session, so there is nobody to ask;
            # the CLI's own dialog is the whole of it.
            await question.answer(None)
            return
        async with entry.lock:
            await self.shared.question(entry, question)

    async def attach_question_closed(self, question: HookQuestion) -> None:
        entry = self.entries.get(question.session_id)
        if entry is None or entry.shared is None:
            return
        async with entry.lock:
            await self.shared.question_closed(entry, question)

    async def attach_turn(self, hook: TurnHook) -> dict[str, str]:
        """A turn hook of an attached CLI (A51); a session nothing is attached to gets nothing."""
        entry = self.entries.get(hook.session_id)
        if entry is None or entry.shared is None:
            return {}
        return await self.shared.turn_hook(entry, hook)

    async def attach_closed(self, attachment: Attachment) -> None:
        """The bridge went away: back to the terminal if the CLI is still alive.

        Not while the daemon is stopping: every bridge goes with it, and a turn the CLI is still
        running is not ended, nor the session handed back, nor its queue let go.
        """
        entry = self.entries.get(attachment.session_id)
        if entry is None or entry.shared is None:
            return
        if not self._closed:
            await asyncio.sleep(EXIT_SETTLE)
        if self._closed:
            # Before the settle or during it: the daemon is stopping, and that is why this bridge
            # went, or the one that went was about to be taken down with the rest.
            async with entry.lock:
                await self.shared.stopped(entry, attachment)
            return
        control = await self._holder_control(entry)
        if control == "none":
            # The CLI is gone, and what its hook said went with it: a later process may be given
            # its pid. A bridge that closes while its CLI lives, as one reconnecting does, leaves
            # the hook's word where it is.
            self.bindings.forget(attachment.pid)
        async with entry.lock:
            await self.shared.closed(entry, attachment, control)
        if control == "none" and self._is_ghost(entry):
            await self.withdraw(entry)

    # --------------------------------------------------- the session behind it

    async def attach_session_started(self, start: SessionStart) -> None:
        """A `SessionStart` hook: the CLI at `start.pid` is now in `start.session_id`.

        The hook is the only account of a `/resume` or a `/clear` the device
        gets: those change the session a terminal is in without restarting
        anything, so the bridge keeps naming the session the CLI started on.
        """
        heard = self.bindings.stamp()
        if not self.bindings.remember(start, await self.process_start(start.pid), heard):
            # A later hook of this CLI got past the look at the process first.
            return
        entry = self._attached_to(start.pid)
        if entry is None or entry.session.session_id == start.session_id:
            return
        await self._move_attachment(entry, start)

    def _attached_to(self, pid: int) -> SessionEntry | None:
        """The session whose live bridge belongs to this CLI process."""
        for entry in self.entries.values():
            state = entry.shared
            if state is not None and state.attachment.pid == pid:
                return entry
        return None

    async def _move_attachment(self, entry: SessionEntry, start: SessionStart) -> None:
        """Carry a live attachment over to the session its terminal has moved to."""
        state = entry.shared
        if state is None:
            return
        attachment = state.attachment
        target = self._terminal_entry(start.session_id, start.cwd, start.transcript_path)
        if target is None:
            return
        log.info("a terminal moved to another session", source=start.source)
        attachment.session_id = start.session_id
        async with entry.lock:
            entry.holder_pid = None
            entry.holder_identity = None
            await self.shared.closed(entry, attachment, "none")
        async with target.lock:
            await self.shared.registered(target, attachment)
        if self._is_ghost(entry):
            await self.withdraw(entry)

    # ------------------------------------------------- ghosts and withdrawals

    def _is_ghost(self, entry: SessionEntry) -> bool:
        """A session a terminal announced and left without ever using it.

        Every interactive `claude` names a session before anyone types into it,
        and picking another one with `/resume` leaves that name behind: no
        transcript is ever written for it and nothing is ever said in it. The
        test for "nothing was said" is the stored history rather than
        `last_seq`, which counts the `meta` and `status` events an attachment
        publishes about itself and is never 0 for a session that was attached.
        """
        session = entry.session
        if session.agent != "claude" or session.origin != "terminal":
            return False
        if entry.shared is not None or entry.runner is not None:
            return False
        if entry.transcript is not None:
            return False
        if self.registry.has_events(session.session_id):
            return False
        return transcripts.find_transcript(session.session_id) is None

    async def sweep_ghosts(self) -> None:
        """Drop every ghost the device is holding, and repeat older removals.

        Run from the mirror's scan, which also runs once the gateway link comes
        up: a `session.removed` published while the link was down was dropped,
        and the gateway keeps every session a device has ever announced, so an
        unrepeated removal would leave the ghost in the apps.
        """
        await self._repeat_removals()
        for entry in list(self.entries.values()):
            if self._is_ghost(entry):
                await self.withdraw(entry)

    async def withdraw(self, entry: SessionEntry) -> None:
        """Remove a session nobody asked about, and keep saying so until a link carries it.

        The gateway keeps every session a device has ever announced, so a
        `session.removed` the device sends of its own accord — a ghost (A16), a
        thread another application owns (A18) — has to survive a link that was
        down when it went out.
        """
        session_id = entry.session.session_id
        await self._remove(entry)
        self._removed.append((self._link, session_id))
        del self._removed[:-MAX_PENDING_REMOVALS]

    async def _repeat_removals(self) -> None:
        """Announce again every removal that went out over an earlier link.

        Only while the link is up: a repeat published into a gap would be
        dropped like the original, and the record with it.
        """
        if not self.link_up():
            return
        current = self._link
        stale = [session_id for link, session_id in self._removed if link < current]
        self._removed = [item for item in self._removed if item[0] >= current]
        for session_id in stale:
            await self.publish({"type": "session.removed", "session_id": session_id})

    async def _holder_control(self, entry: SessionEntry) -> str:
        """Whether the CLI whose bridge just closed is still sitting in the terminal.

        The bridge registered the CLI's own pid, so this asks after that exact
        process. The working directory is deliberately not offered: it cannot
        tell this session's CLI from another one started beside it.
        """
        from ..agents.claude.holders import SessionRef, scan_holders

        scan = await scan_holders()
        if not scan.complete:
            return "terminal"
        session_id = entry.session.session_id
        ref = SessionRef(session_id, pid=entry.holder_pid, identity=entry.holder_identity)
        holder = scan.assign([ref]).get(session_id)
        entry.holder_pid = holder.pid if holder else None
        entry.holder_identity = holder.identity if holder else None
        return "terminal" if holder is not None else "none"
