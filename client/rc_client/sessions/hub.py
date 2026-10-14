"""The session hub: create, drive, queue, mirror and tear down sessions."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import attachments
from ..agents.base import SessionRunner
from ..agents.claude import transcripts
from ..agents.codex.daemon.service import CodexDaemonService
from ..agents.codex.models import catalog_cache
from ..agents.grok.service import GrokLeaderService
from ..agents.pi.adapter import PiRunner
from ..agents.pi.service import PiExtensionService
from ..agents.registry import RunnerSpec, offline_commands, runner_for
from ..errors import RcError
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
from . import titles
from .attach import Attachment, HookQuestion, SessionStart
from .channel import SessionChannel
from .limits import LimitStop
from .ptys import PtyLinks
from .queue import hold, queue_ts_from, snapshot_entry
from .resume import ResumeScheduler
from .shared import EXIT_SETTLE, SharedControl, SharedState

log = logger("rc_client.hub")

Publisher = Callable[[dict[str, Any]], Awaitable[None]]
MAX_TEXT_BYTES = 64 * 1024
MAX_ATTACHMENTS = 8
# How many terminals may be remembered at once, and how many removals may wait
# for a link. Both are bounded because a terminal that never closes its bridge
# and a link that never comes back would otherwise grow them without end.
MAX_TERMINALS = 64
MAX_PENDING_REMOVALS = 64
# How long closing a session may take before the reply goes out anyway (A39).
# An agent that will not die is still let go of here: the person asked for the
# session to be closed, and a request that never answers helps nobody.
CLOSE_TIMEOUT = 8.0


def _as_int(value: Any, field: str) -> int | None:
    """Coerce an app-supplied number, reporting a protocol error when it is not one."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RcError("bad_request", f"{field} must be an integer") from exc


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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        # The session each terminal CLI last said it was in, by its own pid.
        self._terminals: dict[int, SessionStart] = {}
        # Removals the device made on its own initiative, and the link each
        # went out on. A frame published while the link is down is dropped, so
        # each one is said again on the next link.
        self._removed: list[tuple[int, str]] = []
        self._link = 0
        # Whether the gateway link is up right now; the daemon wires the real
        # answer in, so a repeated removal is never published into a gap.
        self.link_up: Callable[[], bool] = lambda: True

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
        return channel

    async def _turn_ended(self, session: Session, limit: LimitStop | None) -> None:
        """Amendment A35: a turn the usage limit ended schedules its own resume."""
        await self.resumes.on_turn_end(session, limit)

    def load(self) -> None:
        """Restore persisted sessions as resumable, terminal-free records.

        `resume` is not restored here: a pending one belongs to the scheduler,
        which reads its own records back and puts them on the sessions (A35).
        """
        for session in self.registry.load_sessions():
            if not session.session_id or not session.agent:
                continue
            session.device_id = self.device_id
            session.control = "none"
            session.state = "stopped" if session.archived else "idle"
            session.turn = None
            session.queued = 0
            session.resume = None
            self.entries[session.session_id] = SessionEntry(
                session=session,
                channel=self._channel(session),
            )

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

    async def close(self) -> None:
        for entry in list(self.entries.values()):
            if entry.shared is not None:
                entry.shared.attachment.detach()
                entry.shared = None
            if entry.runner is not None:
                with contextlib.suppress(Exception):
                    await entry.runner.close()
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
            await self.drain_queue(entry)

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
            await self.resumes.cancelled_by_person(session_id)

        text = str(params.get("text") or "")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise RcError("too_large", "message text exceeds 64 KiB")
        attachments = params.get("attachments") or []
        if len(attachments) > MAX_ATTACHMENTS:
            raise RcError("too_large", "at most 8 attachments per message")
        mode = str(params.get("mode") or "auto")
        # A43: an edited message comes back under the `ts` its queue entry had.
        # It never decides whether the message queues; `mode` does.
        queue_ts = queue_ts_from(params)

        async with entry.lock:
            if entry.shared is not None:
                result = await self.shared.send(
                    entry, request_id, text, attachments, source, queue_ts
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
            elif (
                mode == "auto"
                and runner.supports_steer
                and await runner.steer(text, request_id or None)
            ):
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
            await titles.from_prompt(entry.channel, text)
        await runner.send(text, attachments, source=source, block_id=request_id or None)

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

    async def drain_queue(self, entry: SessionEntry) -> None:
        """Launch the oldest queued message once the agent goes idle."""
        async with entry.lock:
            item = self._take_queued(entry)
        if item is None:
            return
        await self._send_queued(entry, item)

    async def _drain_queue_locked(self, entry: SessionEntry) -> None:
        """Same as `drain_queue`, for callers that already hold the lock."""
        item = self._take_queued(entry)
        if item is not None:
            await self._send_queued(entry, item)

    def _take_queued(self, entry: SessionEntry) -> dict[str, Any] | None:
        if not entry.queue or entry.runner is None or entry.runner.busy:
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
            return await self.shared.approve(
                entry, str(params.get("request_id") or ""), str(params.get("option_id") or "")
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
        title = params.get("title")
        # A `speed` of null is a session going back to the standard tier, so
        # only the key's absence means "leave it alone" (amendment A21).
        speed: SpeedSetting = params.get("speed") if "speed" in params else UNSET
        await self._check_speed(info, speed, model or entry.session.model)
        if self._is_shared(entry):
            await self._set_shared(entry, info, model, permission_mode, effort, speed)
        if entry.runner is not None:
            await entry.runner.apply_settings(model, permission_mode, effort, speed)
        if title is not None:
            title = title_from_text(str(title))
            if title:
                # A title the user typed outranks anything the agent produces.
                titles.pin(self.registry, entry.session.session_id)
        meta: dict[str, Any] = {} if speed is UNSET else {"speed": speed}
        for key, value in (
            ("model", model),
            ("permission_mode", permission_mode),
            ("effort", effort),
            ("title", title),
        ):
            if value is not None:
                meta[key] = value
        await entry.channel.set_meta(**meta)
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
        name = str(params.get("name") or "").strip().lstrip("/")
        if not name:
            raise RcError("bad_request", "name is required")
        argument = str(params.get("argument") or "").strip() or None
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
        before = len(entry.queue)
        entry.queue = [item for item in entry.queue if item["id"] != queued_id]
        if len(entry.queue) == before:
            raise RcError("not_found", "that message is not queued")
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
        take it back out of the Archive. Everything else is one publish at the
        end: the row goes from working to closed and archived in a single step,
        whether or not the agent went quietly.
        """
        runner = entry.runner
        entry.channel.closing = True
        try:
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
            entry.channel.closing = False
        await entry.channel.publish_summary()

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
        await self.shared.forget(entry)
        if entry.runner is not None:
            await entry.runner.close()
        await entry.channel.close()
        self.entries.pop(session_id, None)
        self.registry.delete_session(session_id)
        attachments.cleanup(session_id)
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
        entry = self._attach_entry(attachment)
        if entry is None:
            attachment.detach()
            return
        async with entry.lock:
            await self.shared.registered(entry, attachment)

    def _attach_entry(self, attachment: Attachment) -> SessionEntry | None:
        """The session this bridge speaks for, which is not always the one it names.

        Claude Code hands an MCP server the environment it was spawned with, so
        a bridge reports the id the CLI chose at startup for as long as it runs.
        Where its hook has already said otherwise, the hook is the fresher
        account and the attachment follows it.
        """
        start = self._terminals.get(attachment.pid)
        if start is not None and start.session_id != attachment.session_id:
            attachment.session_id = start.session_id
            return self._terminal_entry(start.session_id, attachment.cwd, start.transcript_path)
        return self._terminal_entry(attachment.session_id, attachment.cwd, "")

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

    async def attach_closed(self, attachment: Attachment) -> None:
        """The bridge went away: back to the terminal if the CLI is still alive."""
        self._terminals.pop(attachment.pid, None)
        entry = self.entries.get(attachment.session_id)
        if entry is None or entry.shared is None:
            return
        await asyncio.sleep(EXIT_SETTLE)
        control = await self._holder_control(entry)
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
        self._remember_terminal(start)
        entry = self._attached_to(start.pid)
        if entry is None or entry.session.session_id == start.session_id:
            return
        await self._move_attachment(entry, start)

    def _remember_terminal(self, start: SessionStart) -> None:
        self._terminals.pop(start.pid, None)
        self._terminals[start.pid] = start
        for pid in list(self._terminals)[:-MAX_TERMINALS]:
            self._terminals.pop(pid, None)

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
