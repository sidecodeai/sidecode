"""Drive one Grok Build session over ACP, on a private child or on the leader.

Two transports, one runner. A session this device started for itself owns an
`agent agent --no-leader stdio` child, exactly as before. A session on the
machine's leader shares one connection with every other client of it (A28): the
device joins with `session/load`, publishes the replay above the `eventId` it
already applied, mirrors the words typed at the TUI as terminal messages, and
sends `session/close`, which unloads the session for everyone, in one place
only — `shutdown`, the close of a session no terminal is in (A39).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...attachments import (
    Attachment,
    describe,
    drop,
    store,
    wire_attachments,
    without_paths,
)
from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting, now_ms
from ...sessions.channel import SessionChannel
from ..base import Emit
from . import cursor
from .acp import GrokAgent, GrokError, GrokTransport
from .catalog import GrokCatalog
from .commands import advertised, recall, remember
from .dialogs import DialogDesk
from .echoes import EchoLog
from .leader import LeaderClient, SessionRoute
from .translate import (
    SETTINGS,
    GrokTranslator,
    config_settings,
    session_update,
    stop_reason,
    text_of,
)

log = logger("rc_client.grok")

DRAIN_TIMEOUT = 15.0
# How long a close waits for the cancelled turn before unloading the session
# anyway (A39). The person is closing it, so the session matters more than the
# last words of the turn.
CLOSE_DRAIN_TIMEOUT = 5.0
LOAD_TIMEOUT = 120.0
# The JSON-RPC errors that say a request was refused as it stood: it was never
# taken up, so a prompt answered with one was never read either.
REFUSED_REQUEST = (-32600, -32601, -32602)
# What follows a bubble whose message Grok was never sent (A49).
UNREAD = "the agent was stopped before it read your message"
# How many finished turns are remembered by prompt id: only the last can still end late.
FINISHED_KEPT = 16

TurnEndCallback = Callable[[], Awaitable[None]]
SessionIdCallback = Callable[[str], Awaitable[None]]


class GrokRunner:
    agent = "grok"

    def __init__(
        self,
        channel: SessionChannel,
        *,
        binary: str,
        cwd: str,
        catalog: GrokCatalog,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        resume: str | None = None,
        leader: LeaderClient | None = None,
        on_turn_end: TurnEndCallback | None = None,
        on_session_id: SessionIdCallback | None = None,
    ) -> None:
        self.channel = channel
        self._binary = binary
        self._cwd = cwd
        self._catalog = catalog
        self._model = model
        self._permission_mode = permission_mode
        self._effort = effort
        self._resume = resume
        self._leader = leader
        self._on_turn_end = on_turn_end
        self._on_session_id = on_session_id
        self._child: GrokAgent | None = None
        self._session_id: str | None = None
        self._translator = GrokTranslator(
            cwd=cwd,
            # On the leader the prompts typed at the TUI arrive as ordinary
            # `user_message_chunk`s and are the only record of what was said.
            mirror_user_messages=leader is not None,
            keep_replay=leader is not None,
        )
        self._echoes = EchoLog()
        self._turn: asyncio.Task[None] | None = None
        # The turn-end callback a finished prompt task set going.
        self._turn_over: asyncio.Future[None] | None = None
        self._turn_started_at = 0
        self._interrupting = False
        self._closed = False
        # The approvals and questions Grok has open, and who else may answer them.
        self._dialogs = DialogDesk(channel, shared=lambda: self.attached and self.terminal_holds)
        self._commands: list[Command] = []
        # The highest `eventId` counter applied, and the one this session started
        # from: everything at or below it is history the apps already have.
        self._cursor = 0
        self._resume_from = 0
        # The prompt most recently sent, which hears whatever Grok says next.
        self._prompt: _Prompt | None = None
        # The turns already over, by Grok's prompt id. A turn ends twice — by its
        # `turn_completed` and by its prompt's answer — and the later of the two
        # may arrive once the next turn has begun.
        self._finished: deque[str] = deque(maxlen=FINISHED_KEPT)
        # The prompt id of the turn under way, as its own rows name it.
        self._turn_prompt: str | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def attached(self) -> bool:
        """Whether this session is held on the machine's shared leader."""
        return self._leader is not None

    @property
    def terminal_holds(self) -> bool:
        """Whether a TUI is in this session, which `control` is the record of (A28)."""
        return self.channel.session.control in {"shared", "terminal"}

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def _transport(self) -> GrokTransport | None:
        if self._closed:
            return None
        return self._leader if self._leader is not None else self._child

    async def start(self) -> None:
        self._closed = False
        self._dialogs.open()
        if self._leader is not None:
            await self._join_leader()
        else:
            await self._start_child()
        assert self._session_id is not None
        self._translator.context_window = self._catalog.context_window(self._model)
        if self._on_session_id:
            await self._on_session_id(self._session_id)

    async def _start_child(self) -> None:
        agent = GrokAgent(
            self._binary,
            cwd=self._cwd,
            args=self._spawn_args(),
            env=sanitized_child_env(),
            on_notification=self._on_notification,
            on_request=self._on_request,
        )
        await agent.start()
        self._child = agent
        params: dict[str, Any] = {"cwd": self._cwd, "mcpServers": []}
        if self._resume:
            params["sessionId"] = self._resume
            self._session_id = self._resume
            self._remember_cursor()
            result = await agent.request("session/load", params)
        else:
            result = await agent.request("session/new", params)
            self._session_id = str(result.get("sessionId") or "")
            self._remember_cursor()
        if not self._session_id:
            raise RcError("agent_unavailable", "grok did not return a session id")
        await self._adopt_settings(result)
        await self._apply_mode(self._permission_mode)

    async def _join_leader(self) -> None:
        """Take a lease on the shared leader, joining or creating one session.

        A `session/load` replays before it answers, so the route is registered
        first; a `session/new` says nothing until it has an id to say it under.
        """
        leader = self._leader
        assert leader is not None
        params: dict[str, Any] = {"cwd": self._cwd, "mcpServers": []}
        if self._resume:
            self._session_id = self._resume
            self._remember_cursor()
            leader.attach(self._resume, self._route())
            params["sessionId"] = self._resume
            result = await leader.request("session/load", params, timeout=LOAD_TIMEOUT)
            await self._adopt_settings(result)
            return
        result = await leader.request("session/new", params)
        session_id = str(result.get("sessionId") or "")
        if not session_id:
            raise RcError("agent_unavailable", "grok did not return a session id")
        self._session_id = session_id
        self._remember_cursor()
        leader.attach(session_id, self._route())
        # Process flags are ignored in leader mode, so what the app asked for is
        # applied to the session itself.
        await self._choose_settings()
        await self._adopt_settings(result)
        await self._apply_mode(self._permission_mode)

    def _route(self) -> SessionRoute:
        return SessionRoute(on_notification=self._on_notification, on_request=self._on_request)

    async def resubscribe(self) -> None:
        """Re-join this session after the leader connection came back (A28)."""
        leader = self._leader
        if leader is None or self._session_id is None:
            return
        self._remember_cursor()
        leader.attach(self._session_id, self._route())
        result = await leader.request(
            "session/load",
            {"sessionId": self._session_id, "cwd": self._cwd, "mcpServers": []},
            timeout=LOAD_TIMEOUT,
        )
        await self._adopt_settings(result)

    def _remember_cursor(self) -> None:
        """Everything at or below this counter has already reached the apps."""
        if self._session_id is None:
            return
        self._resume_from = cursor.read(self.channel.registry, self._session_id)
        self._cursor = max(self._cursor, self._resume_from)

    def _spawn_args(self) -> list[str]:
        """Model and effort are process options; everything else is per session."""
        args: list[str] = []
        if self._model:
            args.extend(["--model", self._model])
        effort = self._catalog.clamp_effort(self._model, self._effort)
        if effort:
            args.extend(["--reasoning-effort", effort])
        return args

    async def _choose_settings(self) -> None:
        """Apply the model and effort a new leader session was asked for."""
        if self._model:
            await self._set_option("model", self._model)
        effort = self._catalog.clamp_effort(self._model, self._effort)
        if effort:
            await self._set_option("reasoning_effort", effort)

    async def _adopt_settings(self, result: dict[str, Any]) -> None:
        """Report what the agent actually runs with, which A17 says apps show."""
        fields = config_settings(result.get("configOptions"))
        model = fields.get("model")
        if isinstance(model, str):
            self._model = model
            self._translator.context_window = self._catalog.context_window(model)
        effort = fields.get("effort")
        if isinstance(effort, str):
            self._effort = effort
        if fields:
            await self.channel.set_meta(**fields)

    async def close(self) -> None:
        # Letting go of a session a terminal shares leaves its open prompts to
        # the terminal: any reply now would settle them for everyone (A52).
        keep = self.attached and self.terminal_holds
        self._closed = True
        await self._dialogs.let_go(keep_for_others=keep)
        if self._turn is not None:
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn
            self._turn = None
        if self._leader is not None:
            # Only stop routing. `session/close` unloads the session for every
            # client of the leader, the terminal included (A28); `shutdown` is
            # where the person's close sends it (A39).
            if self._session_id is not None:
                self._leader.detach(self._session_id)
            return
        if self._child is not None:
            await self._child.close()
            self._child = None

    async def shutdown(self) -> None:
        """End the session for good (A39): on the leader, unload it as well.

        `close` only stops routing, because `session/close` unloads the session
        for every client of the leader, a TUI included (A28). This is the one
        place that sends it, and only for a session no terminal is in: that is
        what `remote` means, and it is the only session an app offers to close.
        A private child needs nothing — closing it ends the agent with it.
        """
        session_id = self._session_id
        if self._leader is None or session_id is None or self.terminal_holds:
            await self.close()
            return
        if self.busy:
            with contextlib.suppress(TimeoutError, RcError):
                await asyncio.wait_for(self.interrupt(), CLOSE_DRAIN_TIMEOUT)
        with contextlib.suppress(RcError):
            await self._leader.request("session/close", {"sessionId": session_id})
        await self.close()

    # ------------------------------------------------------------- streaming

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        self._advance(params)
        if self._replayed(params):
            return
        self._note_prompt(method, params)
        if self._prompt is not None:
            self._prompt.heard = True
        if await self._interaction(method, params):
            return
        if self._note_commands(method, params):
            return
        if self._own_echo(method, params):
            return
        completion: dict[str, Any] | None = None
        for emit in self._translator.notification(method, params):
            if emit.kind == "turn_completed":
                completion = dict(emit.fields)
                continue
            if emit.kind == "user_message":
                await self._terminal_message(emit)
                continue
            await self._apply(emit)
        if completion is not None:
            await self._finish_turn(completion, _completed_prompt(method, params))

    def _advance(self, params: dict[str, Any]) -> None:
        """Remember the highest counter seen, so a later reader resumes from it."""
        index = cursor.index_of(params)
        if index <= self._cursor or self._session_id is None:
            return
        self._cursor = index
        cursor.write(self.channel.registry, self._session_id, index)

    def _note_prompt(self, method: str, params: dict[str, Any]) -> None:
        """Remember which prompt the turn under way belongs to, from its own rows."""
        meta = params.get("_meta")
        prompt_id = meta.get("promptId") if isinstance(meta, dict) else None
        if method == "_x.ai/queue/changed":
            prompt_id = params.get("runningPromptId")
        elif not prompt_id:
            found = session_update(method, params)
            if found is not None and found[0] != "turn_completed":
                prompt_id = found[1].get("prompt_id")
        if isinstance(prompt_id, str) and prompt_id and prompt_id not in self._finished:
            self._turn_prompt = prompt_id

    def _replayed(self, params: dict[str, Any]) -> bool:
        """Whether this row is history the apps already have (A28).

        Only a numbered conversation row can be recognised: the state rows a
        replay carries — the model, the command list — have no counter and are
        applied every time, which is what keeps them current.
        """
        if not cursor.is_replay(params):
            return False
        index = cursor.index_of(params)
        return bool(index) and index <= self._resume_from

    def _own_echo(self, method: str, params: dict[str, Any]) -> bool:
        """Whether this row is the leader's echo of a prompt this device sent.

        Every client of the leader sees every prompt and none of them says who
        sent it, so the device drops the echo of its own and publishes the rest
        as the terminal messages they are. The echo is matched before the
        translator takes the attachment paths off it: the prompt as sent, paths
        and all, is the closest thing to an author the row carries (A49).
        """
        found = session_update(method, params)
        if found is None or found[0] != "user_message_chunk":
            return False
        return self._echoes.claim(text_of(found[1].get("content")))

    async def _terminal_message(self, emit: Emit) -> None:
        """A prompt somebody typed at the TUI: it starts a turn there."""
        await self._apply(emit)
        if self.channel.session.turn is None:
            self._turn_started_at = now_ms()
            await self.channel.begin_turn("terminal")

    async def _interaction(self, method: str, params: dict[str, Any]) -> bool:
        """Grok's own signals that a prompt opened or closed, for any client.

        A question's tool result is watched on its way past as well: when
        somebody else answered, it is the one record of what they chose.
        """
        found = session_update(method, params)
        if found is None:
            return False
        kind, update = found
        if kind == "pending_interaction":
            # The request itself follows: `session/request_permission` or
            # `_x.ai/ask_user_question`.
            return True
        if kind == "interaction_resolved":
            await self._dialogs.resolved_elsewhere(str(update.get("tool_call_id") or ""))
            return True
        if kind == "tool_call_update":
            await self._dialogs.tool_result(update)
        return False

    async def _apply(self, emit: Emit) -> None:
        if emit.kind == SETTINGS:
            model = emit.fields.get("model")
            if isinstance(model, str):
                self._model = model
            effort = emit.fields.get("effort")
            if isinstance(effort, str):
                self._effort = effort
            mode = emit.fields.get("permission_mode")
            if isinstance(mode, str):
                self._permission_mode = mode
            await self.channel.set_meta(**emit.fields)
            return
        if emit.delta:
            fields = dict(emit.fields)
            block_id = str(fields.pop("block_id"))
            delta = str(fields.pop("delta", ""))
            await self.channel.emit_delta(emit.kind, block_id, delta, **fields)
            return
        if emit.kind == "todos":
            await self.channel.publish_todos(list(emit.fields.get("items") or []))
            return
        await self.channel.emit(emit.kind, **emit.fields)

    async def _finish_turn(self, completion: dict[str, Any], prompt_id: str | None = None) -> None:
        if prompt_id:
            if prompt_id in self._finished:
                # The other ending of a turn already over: it must neither end the
                # turn that has begun since nor expire what that turn asks.
                return
            self._finished.append(prompt_id)
        await self._dialogs.turn_over()
        if self.channel.session.turn is None:
            # The prompt's own answer and its `turn_completed` notification both
            # end the turn, and either may arrive first.
            return
        duration = int(completion.get("duration_ms") or 0) or max(
            0, now_ms() - self._turn_started_at
        )
        reason = "interrupted" if self._interrupting else str(completion.get("stop_reason"))
        self._interrupting = False
        usage = completion.get("usage")
        await self.channel.end_turn(
            reason, duration, dict(usage) if isinstance(usage, dict) else None
        )
        if self._on_turn_end is not None and not self.local_turn:
            # A prompt of this device's is only over once its request has
            # answered, which `_prompt_answered` reports.
            await self._on_turn_end()

    # --------------------------------------------------------------- driving

    @property
    def busy(self) -> bool:
        if self._turn is not None and not self._turn.done():
            return True
        # On the leader a turn the terminal started is just as busy: a prompt
        # sent now would queue behind it in Grok itself.
        return self.attached and self.channel.session.turn is not None

    @property
    def local_turn(self) -> bool:
        """Whether a turn this device started is running, for the A28 table."""
        return self._turn is not None and not self._turn.done()

    @property
    def supports_steer(self) -> bool:
        """Grok queues a prompt sent mid-turn; it cannot fold it into the turn."""
        return False

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        return False

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        transport = self._transport
        if transport is None or self._session_id is None:
            raise RcError("agent_unavailable", "the Grok session is not connected")
        # `promptCapabilities.image` is false on this build, so a file travels
        # as a path in the prompt and Grok opens it with `read_file` (A49).
        written = await store(self.channel.session.session_id, attachments)
        prompt = describe(text, written)
        try:
            await self._announce(text, prompt, written, source, block_id)
        except asyncio.CancelledError:
            # Stopped before Grok was asked anything, as a close stops a drain
            # (A39): nothing will read the files, and the turn the bubble
            # opened ends saying the message went unread.
            await drop(written)
            await self._unread()
            raise
        except Exception:
            await drop(written)
            raise
        self._prompt = sent = _Prompt(written)
        # `session/prompt` only answers when the turn is over, so it runs as a
        # task: the hub's caller must come back as soon as the prompt is in.
        self._turn = asyncio.create_task(self._run_turn(transport, prompt, sent), name="grok-turn")
        self._turn.add_done_callback(self._prompt_answered)

    def _prompt_answered(self, turn: asyncio.Task[None]) -> None:
        """The prompt's task is done, so the session is idle for its queue at last.

        Its turn ended inside the task, usually on the `turn_completed` that
        comes before the answer, while the runner still counted as busy; a
        queue drained then would have found nothing it could send (A43). A
        task cancelled by `close` ends nothing: the session is going.
        """
        if turn.cancelled() or self._on_turn_end is None:
            return
        self._turn_over = asyncio.ensure_future(self._on_turn_end())

    async def _announce(
        self, text: str, prompt: str, written: list[Attachment], source: str, block_id: str | None
    ) -> None:
        """The bubble and the turn's start, before Grok is asked anything."""
        if self.attached:
            # The leader will echo this prompt back to every client, this one
            # included; the bubble below is the one the apps keep.
            self._echoes.remember(prompt)
        self._turn_prompt = None
        await self.channel.emit(
            "user_message",
            block_id=block_id or f"user:{uuid.uuid4()}",
            text=text,
            source=source,
            **({"attachments": wire_attachments(written)} if written else {}),
        )
        self._turn_started_at = now_ms()
        await self.channel.begin_turn(source)

    async def _unread(self) -> None:
        """End the turn a message announced and never sent, if it got that far."""
        if self.channel.session.turn is None:
            return
        await self.channel.end_turn("interrupted", max(0, now_ms() - self._turn_started_at))
        await self.channel.notice("warn", UNREAD)

    async def _run_turn(self, transport: GrokTransport, prompt: str, sent: _Prompt) -> None:
        try:
            result = await transport.request(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": [{"type": "text", "text": prompt}]},
                timeout=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if sent.refused(exc):
                # Grok turned the prompt down as it stood and said nothing of
                # it, so nothing will open its files (A49). Any other failure
                # may come after it read them; those wait for the age limit.
                await drop(sent.written)
            await self._fail_turn(exc.message if isinstance(exc, RcError) else str(exc))
            return
        # The `turn_completed` notification normally ends the turn; this closes
        # one the agent answered without sending it.
        prompt_id = _answered_prompt(result)
        if prompt_id in self._finished:
            return
        for emit in self._translator.close_streams():
            await self._apply(emit)
        await self._finish_turn(
            {"stop_reason": stop_reason(str(result.get("stopReason") or "")), "duration_ms": 0},
            prompt_id,
        )

    async def _fail_turn(self, message: str) -> None:
        """A prompt that never returns its result still has to close the turn."""
        if self.channel.session.turn is None:
            return
        for emit in self._translator.close_streams():
            await self._apply(emit)
        # Paths off before the cut, so no half of one survives it (A49).
        await self.channel.error(without_paths(message)[:2000])
        await self._end_here({"stop_reason": "error", "duration_ms": 0})

    async def interrupt(self) -> bool:
        transport = self._transport
        if transport is None or self._session_id is None:
            return False
        turn = self._turn
        if turn is None or turn.done():
            return await self._interrupt_terminal(transport)
        self._interrupting = True
        await self.channel.set_state("running", "interrupting")
        self._dialogs.stop_all()
        # ACP cancellation is a notification: the prompt request answers with
        # its own stop reason once the agent has unwound the turn.
        await transport.notify("session/cancel", {"sessionId": self._session_id})
        try:
            await asyncio.wait_for(asyncio.shield(turn), timeout=DRAIN_TIMEOUT)
        except TimeoutError:
            await self._did_not_settle()
        return True

    async def _interrupt_terminal(self, transport: GrokTransport) -> bool:
        """Stop a turn somebody started at the TUI; the leader ends it for all."""
        if not self.attached or self.channel.session.turn is None:
            return False
        self._interrupting = True
        await self.channel.set_state("running", "interrupting")
        self._dialogs.stop_all()
        await transport.notify("session/cancel", {"sessionId": self._session_id})
        return True

    async def _did_not_settle(self) -> None:
        """The agent kept going after a cancel. A private child is restarted.

        A session on the leader is not: restarting it would mean unloading the
        session every other client is in. The turn is closed here instead and
        the leader's own `turn_completed`, whenever it comes, finds nothing open.
        """
        log.warning("grok did not settle after cancel")
        await self.channel.notice("warn", "the agent did not stop in time")
        if self.attached:
            await self._end_here({"stop_reason": "interrupted", "duration_ms": 0})
            return
        await self.channel.notice("warn", "reconnecting to the agent")
        await self._restart()

    async def _restart(self) -> None:
        session_id = self._session_id
        await self.close()
        self._resume = session_id
        await self.start()
        await self._end_here({"stop_reason": "interrupted", "duration_ms": 0})

    async def _end_here(self, completion: dict[str, Any]) -> None:
        """End a turn Grok has not ended, which its own ending may still follow.

        That ending names the prompt, and so does every row of the turn: the id
        is remembered as finished, so the ending finds nothing to end when it
        comes. A turn Grok never said anything about has no id to remember, and
        Grok names it nowhere else; if such a turn's ending arrives late after
        all, it is taken for the turn under way then, as before (A52).
        """
        prompt_id = self._turn_prompt
        if prompt_id and prompt_id not in self._finished:
            self._finished.append(prompt_id)
        await self._finish_turn(completion)

    # -------------------------------------------------------- slash commands

    def _note_commands(self, method: str, params: dict[str, Any]) -> bool:
        """Grok pushes its whole command list when a session opens, and again
        whenever plugins or skills are reloaded (A27). It is state, not an event,
        so a replayed one during `session/load` is taken as gladly as a live one.
        """
        found = session_update(method, params)
        if found is None or found[0] != "available_commands_update":
            return False
        listed = advertised(found[1])
        if listed != self._commands:
            self._commands = listed
            remember(listed)
        return True

    async def commands(self) -> list[Command]:
        """What this session advertised, falling back to what the device last saw.

        The advertisement is a notification, so a `session.commands` that arrives
        in the same instant as the session opens would otherwise answer nothing.
        """
        return list(self._commands) if self._commands else recall()

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        """Grok runs its own commands: the turn's text is `/name argument`."""
        listed = await self.commands()
        if not any(command.name == name for command in listed):
            raise RcError("not_found", f"/{name} is not a command this session offers")
        await self.send(f"/{name} {argument}" if argument else f"/{name}", block_id=block_id)

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        if model is not None:
            self._model = model
            await self._set_option("model", model)
        if effort is not None:
            self._effort = effort
            clamped = self._catalog.clamp_effort(self._model, effort)
            if clamped:
                await self._set_option("reasoning_effort", clamped)
        if permission_mode is not None:
            self._permission_mode = permission_mode
            await self._apply_mode(permission_mode)
        if speed is not UNSET and speed is not None:
            raise RcError("unsupported", "grok has no speed tiers")

    async def _set_option(self, option: str, value: str) -> None:
        """Live model and effort changes; the reply is the whole option list.

        On the leader this reaches every client, the TUI included, which is what
        `shared_settings: true` promises (4.2).
        """
        transport = self._transport
        if transport is None or self._session_id is None:
            return
        result = await transport.request(
            "session/set_config_option",
            {"sessionId": self._session_id, "configId": option, "value": value},
        )
        fields = config_settings(result.get("configOptions"))
        if fields:
            await self.channel.set_meta(**fields)

    async def _apply_mode(self, mode: str | None) -> None:
        transport = self._transport
        if transport is None or self._session_id is None or not mode:
            return
        await transport.request("session/set_mode", {"sessionId": self._session_id, "modeId": mode})
        await self.channel.set_meta(permission_mode=mode)

    # ------------------------------------------------------- prompts to user

    async def _on_request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """An approval or a question Grok asks; `None` leaves it to another client."""
        return await self._dialogs.request(method, params)

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        return await self._dialogs.approve(request_id, option_id, message)

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        """An app's answer to a question `_x.ai/ask_user_question` asked (A52)."""
        return await self._dialogs.answer(request_id, answers)


def _completed_prompt(method: str, params: dict[str, Any]) -> str | None:
    """The prompt a `turn_completed` ends, as Grok names it."""
    found = session_update(method, params)
    prompt_id = found[1].get("prompt_id") if found is not None else None
    return prompt_id if isinstance(prompt_id, str) and prompt_id else None


def _answered_prompt(result: dict[str, Any]) -> str | None:
    """The prompt a `session/prompt` answer ends, as Grok names it in `_meta`."""
    meta = result.get("_meta")
    prompt_id = meta.get("promptId") if isinstance(meta, dict) else None
    return prompt_id if isinstance(prompt_id, str) and prompt_id else None


@dataclass(slots=True)
class _Prompt:
    """One prompt that went out, its files, and whether Grok has said anything since."""

    written: list[Attachment]
    heard: bool = False

    def refused(self, error: Exception) -> bool:
        """Whether the prompt's error says Grok never took it up."""
        code = error.rpc_code if isinstance(error, GrokError) else None
        return not self.heard and code in REFUSED_REQUEST
