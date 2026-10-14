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
from collections.abc import Awaitable, Callable
from typing import Any

from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting, now_ms
from ...sessions.channel import SessionChannel
from ..base import Emit
from . import cursor
from .acp import GrokAgent, GrokTransport
from .catalog import GrokCatalog
from .commands import advertised, recall, remember
from .echoes import EchoLog
from .leader import LeaderClient, SessionRoute
from .translate import SETTINGS, GrokTranslator, config_settings, session_update, stop_reason

log = logger("rc_client.grok")

APPROVAL_TIMEOUT = 300.0
DRAIN_TIMEOUT = 15.0
# How long a close waits for the cancelled turn before unloading the session
# anyway (A39). The person is closing it, so the session matters more than the
# last words of the turn.
CLOSE_DRAIN_TIMEOUT = 5.0
LOAD_TIMEOUT = 120.0
PERMISSION_REQUEST = "session/request_permission"
# The option that turns the whole session into always-approve mode. It is a
# permission policy, so it belongs to `session.set` and never to an approval
# button an app draws on the person's behalf (A27, A28).
ALWAYS_APPROVE = "enable-always-approve"
# What an approval that somebody else answered resolves as (A28, as for Codex).
ELSEWHERE = "elsewhere"

# ACP option kinds, and how firmly each reads in an app.
_OPTION_STYLES = {
    "allow_once": "primary",
    "allow_always": "secondary",
    "reject_once": "danger",
    "reject_always": "secondary",
}

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
        self._turn_started_at = 0
        self._interrupting = False
        self._closed = False
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # The approval each of the leader's tool calls opened, so an answer from
        # another client can close ours.
        self._tool_calls: dict[str, str] = {}
        self._commands: list[Command] = []
        # The highest `eventId` counter applied, and the one this session started
        # from: everything at or below it is history the apps already have.
        self._cursor = 0
        self._resume_from = 0

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
        self._closed = True
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._tool_calls.clear()
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
        if await self._interaction(method, params):
            return
        if self._note_commands(method, params):
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
            await self._finish_turn(completion)

    def _advance(self, params: dict[str, Any]) -> None:
        """Remember the highest counter seen, so a later reader resumes from it."""
        index = cursor.index_of(params)
        if index <= self._cursor or self._session_id is None:
            return
        self._cursor = index
        cursor.write(self.channel.registry, self._session_id, index)

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

    async def _terminal_message(self, emit: Emit) -> None:
        """A prompt somebody typed, unless it is the echo of one we sent.

        Every client of the leader sees every prompt and none of them says who
        sent it, so the device drops the echo of its own and publishes the rest
        as the terminal messages they are.
        """
        text = str(emit.fields.get("text") or "")
        if self._echoes.claim(text):
            return
        await self._apply(emit)
        if self.channel.session.turn is None:
            self._turn_started_at = now_ms()
            await self.channel.begin_turn("terminal")

    async def _interaction(self, method: str, params: dict[str, Any]) -> bool:
        """Grok's own signals that an approval opened or closed, for any client."""
        found = session_update(method, params)
        if found is None:
            return False
        kind, update = found
        if kind == "pending_interaction":
            # The request itself arrives as `session/request_permission`.
            return True
        if kind != "interaction_resolved":
            return False
        await self._resolved_elsewhere(str(update.get("tool_call_id") or ""))
        return True

    async def _resolved_elsewhere(self, tool_call_id: str) -> None:
        """Somebody else answered an approval this device is still showing."""
        request_id = self._tool_calls.get(tool_call_id)
        if not request_id:
            return
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result({ELSEWHERE: True})

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

    async def _finish_turn(self, completion: dict[str, Any]) -> None:
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
        if self._on_turn_end is not None:
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
        if attachments:
            # `promptCapabilities.image` is false on this build, so there is
            # nowhere for a file to go; the hub refuses before this is reached.
            raise RcError("unsupported", "grok sessions cannot carry attachments")
        if self.attached:
            # The leader will echo this prompt back to every client, this one
            # included; the bubble below is the one the apps keep.
            self._echoes.remember(text)
        await self.channel.emit(
            "user_message",
            block_id=block_id or f"user:{uuid.uuid4()}",
            text=text,
            source=source,
        )
        self._turn_started_at = now_ms()
        await self.channel.begin_turn(source)
        # `session/prompt` only answers when the turn is over, so it runs as a
        # task: the hub's caller must come back as soon as the prompt is in.
        self._turn = asyncio.create_task(self._run_turn(transport, text), name="grok-turn")

    async def _run_turn(self, transport: GrokTransport, text: str) -> None:
        try:
            result = await transport.request(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=None,
            )
        except asyncio.CancelledError:
            raise
        except RcError as exc:
            await self._fail_turn(exc.message)
            return
        except Exception as exc:
            await self._fail_turn(str(exc))
            return
        # The `turn_completed` notification normally ends the turn; this closes
        # one the agent answered without sending it.
        for emit in self._translator.close_streams():
            await self._apply(emit)
        await self._finish_turn(
            {"stop_reason": stop_reason(str(result.get("stopReason") or "")), "duration_ms": 0}
        )

    async def _fail_turn(self, message: str) -> None:
        """A prompt that never returns its result still has to close the turn."""
        if self.channel.session.turn is None:
            return
        for emit in self._translator.close_streams():
            await self._apply(emit)
        await self.channel.error(message[:2000])
        await self._finish_turn({"stop_reason": "error", "duration_ms": 0})

    async def interrupt(self) -> bool:
        transport = self._transport
        if transport is None or self._session_id is None:
            return False
        turn = self._turn
        if turn is None or turn.done():
            return await self._interrupt_terminal(transport)
        self._interrupting = True
        await self.channel.set_state("running", "interrupting")
        self._cancel_pending()
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
        self._cancel_pending()
        await transport.notify("session/cancel", {"sessionId": self._session_id})
        return True

    def _cancel_pending(self) -> None:
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result({"cancelled": True})
            self._pending.pop(request_id, None)

    async def _did_not_settle(self) -> None:
        """The agent kept going after a cancel. A private child is restarted.

        A session on the leader is not: restarting it would mean unloading the
        session every other client is in. The turn is closed here instead and
        the leader's own `turn_completed`, whenever it comes, finds nothing open.
        """
        log.warning("grok did not settle after cancel")
        await self.channel.notice("warn", "the agent did not stop in time")
        if self.attached:
            await self._finish_turn({"stop_reason": "interrupted", "duration_ms": 0})
            return
        await self.channel.notice("warn", "reconnecting to the agent")
        await self._restart()

    async def _restart(self) -> None:
        session_id = self._session_id
        await self.close()
        self._resume = session_id
        await self.start()
        await self._finish_turn({"stop_reason": "interrupted", "duration_ms": 0})

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

    async def _on_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == PERMISSION_REQUEST:
            return await self._handle_permission(params)
        raise RcError("unsupported", f"unhandled grok request {method}")

    async def _handle_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        options = _approval_options(params.get("options"))
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        tool_call_id = str(call.get("toolCallId") or "")
        if tool_call_id:
            self._tool_calls[tool_call_id] = request_id
        base: dict[str, Any] = {
            "block_id": f"approval:{request_id}",
            "request_id": request_id,
            "tool": str(call.get("title") or call.get("kind") or "Tool"),
            "tool_kind": "other",
            "title": _permission_title(call),
            "options": options,
        }
        raw_input = call.get("rawInput")
        if isinstance(raw_input, dict):
            base["input"] = raw_input
        await self.channel.emit("approval", status="pending", **base)
        await self.channel.set_state("needs_approval")
        try:
            decision = await self._wait_for(request_id)
        finally:
            self._tool_calls.pop(tool_call_id, None)
        if decision is not None and decision.get(ELSEWHERE):
            # Another client of the leader answered; the turn is already moving.
            await self.channel.emit(
                "approval",
                status="resolved",
                decision={"option_id": ELSEWHERE, "by": "terminal"},
                **base,
            )
            await self.channel.set_state("running")
            return {"outcome": {"outcome": "cancelled"}}
        allowed = {option["id"] for option in options}
        option_id = str(decision.get("option_id") or "") if decision else ""
        if option_id not in allowed:
            # An id we never advertised must never be read as consent.
            if option_id:
                log.warning("unknown grok approval option; cancelling")
            decision = None
        await self.channel.emit(
            "approval",
            status="resolved" if decision else "expired",
            **({"decision": {"option_id": option_id, "by": "remote"}} if decision else {}),
            **base,
        )
        await self.channel.set_state("running")
        if decision is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    async def _wait_for(self, request_id: str) -> dict[str, Any] | None:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            answer = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            if future.cancelled():
                return None
            raise
        finally:
            self._pending.pop(request_id, None)
        return None if answer.get("cancelled") else answer

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result({"option_id": option_id, "message": message})
        return True

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        """Grok asks its questions through a tool, not through ACP; nothing waits here."""
        return False


def _approval_options(options: Any) -> list[dict[str, str]]:
    """The options the agent offered, styled so an app always has both poles.

    The one that switches the session into always-approve mode is left out: it
    is a permission policy for `session.set`, not a button to press on somebody
    else's behalf, and its ACP kind calls it an ordinary single allow (A28).
    """
    styled: list[dict[str, str]] = []
    for option in options or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("optionId") or option.get("id") or "")
        if not option_id or option_id == ALWAYS_APPROVE:
            continue
        kind = str(option.get("kind") or "")
        styled.append(
            {
                "id": option_id,
                "label": str(option.get("name") or option_id),
                "style": _OPTION_STYLES.get(kind, "secondary"),
            }
        )
    if not styled:
        return [
            {"id": "allow", "label": "Allow", "style": "primary"},
            {"id": "reject", "label": "Deny", "style": "danger"},
        ]
    styles = {option["style"] for option in styled}
    if "primary" not in styles:
        styled[0]["style"] = "primary"
    if "danger" not in styles:
        styled[-1]["style"] = "danger"
    return styled


def _permission_title(call: dict[str, Any]) -> str:
    raw = call.get("rawInput")
    if isinstance(raw, dict):
        for key in ("command", "file_path", "path", "query"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().splitlines()[0][:200]
    title = str(call.get("title") or "")
    return title.splitlines()[0][:200] if title else "run a tool"
