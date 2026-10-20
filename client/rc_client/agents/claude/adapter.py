"""Drive one Claude Code session through `claude-agent-sdk`.

Two rules from the upstream recon shape this file: exactly one consumer of
`receive_messages()`, and `interrupt()` must be followed by draining until the
terminal `ResultMessage` under a single absolute deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ToolPermissionContext,
)
from claude_agent_sdk.types import PermissionRuleValue

from ...attachments import Attachment, describe, drop, store, wire_attachments
from ...child_env import child_env_tombstones
from ...config import DEFAULT_CLAUDE_SETTING_SOURCES
from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting, now_ms
from ...sessions.channel import SessionChannel
from ...sessions.limits import LimitStop, claude_transcript_limit
from ..base import Emit
from ..questions import answers_by_prompt, normalise_questions
from . import subagents, transcripts
from .commands import COMMANDS, COMPACT
from .questions import QUESTION_TOOL
from .tools import reads_session_file
from .translate import ClaudeTranslator

log = logger("rc_client.claude")

APPROVAL_TIMEOUT = 30 * 60.0
DRAIN_TIMEOUT = 15.0
DEFAULT_MODEL_ID = "default"
# What follows a bubble whose message the CLI never got (A49).
UNREAD = "the agent was stopped before it read your message"
# How often a turn held open for its subagents asks whether they are done.
SUBAGENT_POLL_S = 5.0

TurnEndCallback = Callable[[], Awaitable[None]]


def _approval_options() -> list[dict[str, str]]:
    return [
        {"id": "allow", "label": "Allow", "style": "primary"},
        {"id": "allow_session", "label": "Allow for this session", "style": "secondary"},
        {"id": "deny", "label": "Deny", "style": "danger"},
    ]


class ClaudeRunner:
    """One live Claude session: the SDK client, its pump and pending prompts."""

    agent = "claude"

    def __init__(
        self,
        channel: SessionChannel,
        *,
        binary: str | None,
        cwd: str,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        resume: str | None = None,
        session_id: str | None = None,
        setting_sources: list[str] | None = None,
        on_turn_end: TurnEndCallback | None = None,
        on_session_id: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.channel = channel
        self._binary = binary
        self._cwd = cwd
        self._model = model
        self._permission_mode = permission_mode
        self._effort = effort
        self._applied_effort = effort
        self._resume = resume
        self._session_id = session_id
        self._setting_sources = (
            list(setting_sources)
            if setting_sources is not None
            else list(DEFAULT_CLAUDE_SETTING_SOURCES)
        )
        self._on_turn_end = on_turn_end
        self._on_session_id = on_session_id
        self._client: ClaudeSDKClient | None = None
        self._pump: asyncio.Task[None] | None = None
        self._translator = ClaudeTranslator(cwd=cwd)
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._turn_started_at = 0
        self._interrupting = False
        # A turn whose result has arrived while the subagents it started are
        # still running: the completion waiting to be published, and the task
        # watching for the last of them.
        self._held: dict[str, Any] | None = None
        self._hold: asyncio.Task[None] | None = None
        self._subagents: subagents.SubagentWatch | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reported_session_id: str | None = None
        self._resolved_locally = False
        self._closed = False

    # ------------------------------------------------------------- lifecycle

    def _options(self) -> ClaudeAgentOptions:
        model = None if self._model in (None, DEFAULT_MODEL_ID) else self._model
        options: dict[str, Any] = {
            "cwd": self._cwd,
            "include_partial_messages": True,
            "can_use_tool": self._can_use_tool,
            "env": child_env_tombstones(),
            "stderr": self._on_stderr,
            # Without this the CLI loads the machine's user-level settings, and
            # auto-approval there (`permissions.defaultMode: "auto"`, or a
            # PermissionRequest hook) resolves the request in-process and
            # cancels our `can_use_tool` call, so a remote user never decides.
            "setting_sources": self._setting_sources,
        }
        if model:
            options["model"] = model
        if self._permission_mode:
            options["permission_mode"] = self._permission_mode
        if self._effort:
            options["effort"] = self._effort
        if self._resume:
            options["resume"] = self._resume
        elif self._session_id:
            # Claude only reports its session id after the first prompt, so a session created
            # remotely names itself up front: PROTOCOL.md section 0 makes `session_id` the id the
            # app subscribes with, and it must not change under a live subscription.
            options["session_id"] = self._session_id
        if self._binary:
            options["cli_path"] = self._binary
        return ClaudeAgentOptions(**options)

    def _on_stderr(self, line: str) -> None:
        log.debug("claude stderr", chars=len(line))

    async def start(self) -> None:
        self._client = ClaudeSDKClient(options=self._options())
        await self._client.connect()
        self._applied_effort = self._effort
        self._pump = asyncio.create_task(self._pump_loop())

    async def close(self) -> None:
        self._closed = True
        await self._stop_hold()
        self._held = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None

    async def shutdown(self) -> None:
        """A39: the CLI is this device's own, so letting go of it ends it."""
        await self.close()

    async def _reconnect(self) -> None:
        """Rebuild the SDK connection, resuming the same transcript."""
        resume = self._translator.session_id or self._resume
        await self.close()
        self._closed = False
        self._resume = resume
        self._translator = ClaudeTranslator(cwd=self._cwd)
        self._translator.session_id = resume
        await self.start()

    # ------------------------------------------------------------------ pump

    async def _pump_loop(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            async for message in client.receive_messages():
                await self._consume(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("claude message pump stopped", error=type(exc).__name__)
            if not self._closed:
                await self.channel.error("the Claude session ended unexpectedly", code="internal")
                await self.channel.set_state("error", "agent connection lost")
                self._turn_done.set()

    async def _consume(self, message: Any) -> None:
        completion: dict[str, Any] | None = None
        for emit in self._translator.feed(message):
            if emit.kind == "turn_completed":
                completion = dict(emit.fields)
                continue
            await self._apply(emit)
        captured = self._translator.session_id
        if captured and captured != self._reported_session_id and self._on_session_id:
            self._reported_session_id = captured
            await self._on_session_id(captured)
        if completion is not None:
            await self._finish_turn(completion)

    async def _apply(self, emit: Emit) -> None:
        if emit.delta:
            fields = dict(emit.fields)
            block_id = str(fields.pop("block_id"))
            delta = str(fields.pop("delta", ""))
            await self.channel.emit_delta(emit.kind, block_id, delta, **fields)
            return
        if emit.kind == "todos":
            await self.channel.publish_todos(list(emit.fields.get("items") or []))
            return
        if emit.kind == "meta":
            await self.channel.set_meta(**emit.fields)
            return
        await self.channel.emit(emit.kind, **emit.fields)

    async def _finish_turn(self, completion: dict[str, Any]) -> None:
        """End the turn, unless work this session started is still running.

        Working means all of it (`docs/DESIGN.md`): a parent that
        hands its work to background subagents and ends its own turn is still
        working, so the turn — and everything hanging off its end, the
        `turn_completed` event, the gateway's "Turn finished" push, the drain of
        the held queue — waits for the last of them. The waiting runs off the
        message pump so the continuation the CLI streams when a subagent's
        result comes back still arrives, and it belongs to this same turn: a
        second result while one is held replaces the stop reason rather than
        starting anything. A turn the person interrupted is never held, because
        they said stop, and because `interrupt` is already waiting on it.
        """
        self._held = completion
        if self._hold is not None:
            return
        if self._interrupting or not await self._subagents_working():
            await self._close_turn(int(completion.get("duration_ms") or 0))
            return
        self._hold = asyncio.create_task(self._hold_for_subagents())

    async def _hold_for_subagents(self) -> None:
        """Poll the subagent transcripts until the last of them has finished."""
        try:
            while await self._subagents_working():
                await asyncio.sleep(SUBAGENT_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("subagent watch failed; ending the turn")
        self._hold = None
        await self._close_turn(0)

    async def _stop_hold(self) -> None:
        """Drop the task watching the subagents; the turn ends without them."""
        hold, self._hold = self._hold, None
        if hold is None or hold is asyncio.current_task():
            return
        hold.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hold

    async def _close_turn(self, duration_ms: int) -> None:
        """Publish the completion that has been waiting, and let the queue drain.

        `duration_ms` of zero means measure it here, which is what a turn held
        open for its subagents reports: the result's own figure covers the CLI's
        turn alone, and the person watched all of it.
        """
        completion, self._held = self._held, None
        if completion is None:
            return
        usage = dict(completion.get("usage") or {})
        usage.update(await self._context_usage())
        duration = duration_ms or max(0, now_ms() - self._turn_started_at)
        self._interrupting = False
        self._translator.clear_interrupt()
        limit = completion.get("limit")
        if isinstance(limit, LimitStop):
            limit = await self._limit_reset(limit)
        else:
            limit = None
        await self.channel.end_turn(
            str(completion["stop_reason"]), duration, usage or None, limit=limit
        )
        self._turn_done.set()
        if self._on_turn_end is not None:
            await self._on_turn_end()

    async def _subagents_working(self) -> bool:
        """Whether any subagent of this session has not finished yet."""
        watch = self._subagents
        if watch is None:
            session_id = self._translator.session_id or self.channel.session.session_id
            path = await asyncio.to_thread(transcripts.find_transcript, session_id)
            if path is None:
                return False
            watch = self._subagents = subagents.SubagentWatch.for_transcript(path)
        return await asyncio.to_thread(watch.working)

    async def _limit_reset(self, limit: LimitStop) -> LimitStop:
        """Fill in when the window resets, which only the transcript records (A35).

        The SDK's result says the request was refused with 429 and no more; the
        CLI writes the window and its reset into the session's own transcript,
        whose path the device already knows.
        """
        session_id = self._translator.session_id or self.channel.session.session_id
        path = transcripts.find_transcript(session_id)
        if path is None:
            return limit
        found = await asyncio.to_thread(claude_transcript_limit, path)
        return found or limit

    async def _context_usage(self) -> dict[str, Any]:
        client = self._client
        if client is None:
            return {}
        try:
            usage = await asyncio.wait_for(client.get_context_usage(), timeout=5.0)
        except Exception:
            return {}
        total = usage.get("totalTokens")
        window = usage.get("rawMaxTokens") or usage.get("maxTokens")
        result: dict[str, Any] = {}
        if isinstance(total, int):
            result["context_used"] = total
        if isinstance(window, int):
            result["context_window"] = window
        return result

    # --------------------------------------------------------------- driving

    @property
    def busy(self) -> bool:
        return not self._turn_done.is_set()

    @property
    def supports_steer(self) -> bool:
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
        """Publish the message, then start the turn that answers it.

        A message whose prompt never reaches the CLI takes its files with it
        (A49): nothing will ever read the paths. That includes a send stopped
        on the way, as a close stops a drain (A39), whose bubble then says the
        message went unread; the cancellation itself goes on.
        """
        if self._client is None:
            raise RcError("agent_unavailable", "the Claude session is not connected")
        written: list[Attachment] = await store(self.channel.session.session_id, attachments)
        prompt = describe(text, written)
        shown = False
        try:
            # The message goes out before the SDK is touched. Applying a new
            # effort level restarts Claude, which takes seconds, and the apps
            # have drawn this bubble already.
            await self.channel.emit(
                "user_message",
                block_id=block_id or f"user:{uuid.uuid4()}",
                text=text,
                source=source,
                **({"attachments": wire_attachments(written)} if written else {}),
            )
            shown = True
            await self._query(prompt, source)
        except asyncio.CancelledError:
            await drop(written)
            if shown:
                await self.channel.notice("warn", UNREAD)
            raise
        except Exception:
            await drop(written)
            raise

    async def _query(self, prompt: str, source: str) -> None:
        """Hand the CLI the prompt, restarting it first for a new effort level."""
        if self._effort != self._applied_effort:
            await self.channel.notice("info", "restarting Claude to apply the new effort level")
            await self._reconnect()
        client = self._client
        if client is None:
            raise RcError("agent_unavailable", "the Claude session is not connected")
        self._turn_done.clear()
        self._turn_started_at = now_ms()
        try:
            await self.channel.begin_turn(source)
            await client.query(prompt)
        except asyncio.CancelledError:
            await self._abandon_turn("interrupted")
            raise
        except Exception as exc:
            await self._abandon_turn("error")
            raise RcError("internal", f"claude query failed: {type(exc).__name__}") from exc

    async def _abandon_turn(self, stop_reason: str) -> None:
        """End a turn whose prompt never got to the CLI: no result is coming to end it."""
        self._turn_done.set()
        if self.channel.session.turn is not None:
            await self.channel.end_turn(stop_reason, max(0, now_ms() - self._turn_started_at))

    async def interrupt(self) -> bool:
        client = self._client
        if client is None or not self.busy:
            return False
        if self._hold is not None:
            # The CLI's own turn is long over; what is still running are the
            # subagents it started, which it no longer reports on. There is
            # nothing on the SDK to interrupt, so the turn the device held open
            # ends here, as interrupted. The translator is left alone: no
            # further result is coming for a turn the CLI has already ended.
            await self._stop_hold()
            if self._held is not None:
                self._held["stop_reason"] = "interrupted"
                # A limit is only ever published beside an `error` stop (A35).
                self._held.pop("limit", None)
            await self._close_turn(0)
            return True
        self._interrupting = True
        self._translator.mark_interrupted()
        await self.channel.set_state("running", "interrupting")
        # A turn parked on an approval can never reach its ResultMessage, so
        # release every pending prompt before starting the drain clock.
        self._cancel_pending("Interrupted before the user answered.")
        try:
            await client.interrupt()
        except Exception:
            log.warning("claude interrupt call failed; draining anyway")
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=DRAIN_TIMEOUT)
        except TimeoutError:
            log.warning("claude drain timed out; reconnecting the session")
            await self.channel.notice("warn", "the agent did not stop in time; reconnecting")
            await self._reconnect()
            self._turn_done.set()
            await self.channel.end_turn("interrupted", max(0, now_ms() - self._turn_started_at))
        return True

    async def commands(self) -> list[Command]:
        return list(COMMANDS)

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        """A27: the CLI interprets its own commands, so the text is the turn."""
        if name != COMPACT.name:
            raise RcError("not_found", f"/{name} is not a command this session offers")
        await self.send(f"/{name}", block_id=block_id)

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        if speed is not UNSET:
            # Claude advertises no speed tiers, so the hub refuses one long
            # before it reaches here; anything that arrives is a bug, not a
            # setting to apply silently.
            raise RcError("unsupported", "claude has no speed tiers")
        client = self._client
        if model is not None and model != self._model:
            self._model = model
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.set_model(None if model == DEFAULT_MODEL_ID else model)
        if permission_mode is not None and permission_mode != self._permission_mode:
            self._permission_mode = permission_mode
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.set_permission_mode(permission_mode)  # type: ignore[arg-type]
        if effort is not None and effort != self._effort:
            self._effort = effort

    # ------------------------------------------------------- prompts to user

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == QUESTION_TOOL:
            return await self._ask_question(tool_input)
        if reads_session_file(self.channel.session.session_id, tool_name, tool_input):
            # A49: an attachment the prompt named, which is the session's own.
            return PermissionResultAllow()
        return await self._ask_approval(tool_name, tool_input, context)

    async def _wait_for(self, request_id: str, timeout: float) -> dict[str, Any] | None:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            # Our own future being cancelled means "no answer"; being cancelled
            # from outside means shutdown, which must propagate.
            if future.cancelled() and not self._closed:
                return None
            # The CLI withdrew the request, which happens when something on this
            # machine decided the permission for us. Record it so the card does
            # not just expire without explanation.
            self._resolved_locally = True
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _ask_approval(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        from .tools import tool_kind, tool_title

        request_id = str(uuid.uuid4())
        block_id = f"approval:{request_id}"
        base: dict[str, Any] = {
            "block_id": block_id,
            "request_id": request_id,
            "tool": tool_name,
            "tool_kind": tool_kind(tool_name),
            "title": context.title or tool_title(tool_name, tool_input, self._cwd),
            "input": tool_input,
            "options": _approval_options(),
        }
        await self.channel.emit("approval", status="pending", **base)
        await self.channel.set_state("needs_approval")
        decision = await self._wait_for(request_id, APPROVAL_TIMEOUT)
        if decision is None:
            await self.channel.emit("approval", status="expired", **base)
            await self.channel.set_state("running")
            if self._resolved_locally:
                self._resolved_locally = False
                await self.channel.notice(
                    "warn",
                    "this machine answered the permission request before you could; "
                    "check the Claude settings on the device",
                )
            return PermissionResultDeny(message="No response from the remote user; denied.")
        option_id = str(decision.get("option_id") or "deny")
        if option_id not in {option["id"] for option in _approval_options()}:
            # An id we never advertised must never be read as consent.
            log.warning("unknown approval option; denying", tool=tool_name)
            option_id = "deny"
        await self.channel.emit(
            "approval",
            status="resolved",
            decision={"option_id": option_id, "by": "remote"},
            **base,
        )
        await self.channel.set_state("running")
        if option_id == "deny":
            return PermissionResultDeny(message=str(decision.get("message") or "Denied remotely."))
        if option_id == "allow_session":
            return PermissionResultAllow(
                updated_permissions=[
                    PermissionUpdate(
                        type="addRules",
                        rules=[PermissionRuleValue(tool_name=tool_name)],
                        behavior="allow",
                        destination="session",
                    )
                ]
            )
        return PermissionResultAllow()

    async def _ask_question(
        self, tool_input: dict[str, Any]
    ) -> PermissionResultAllow | PermissionResultDeny:
        try:
            questions = normalise_questions(tool_input)
        except ValueError as exc:
            return PermissionResultDeny(message=f"Unusable question payload: {exc}")
        request_id = str(uuid.uuid4())
        block_id = f"question:{request_id}"
        base = {"block_id": block_id, "request_id": request_id, "questions": questions}
        await self.channel.emit("question", status="pending", **base)
        await self.channel.set_state("needs_input")
        reply = await self._wait_for(request_id, APPROVAL_TIMEOUT)
        if reply is None:
            await self.channel.emit("question", status="expired", **base)
            await self.channel.set_state("running")
            return PermissionResultDeny(message="No answer from the remote user.")
        answers = reply.get("answers") or {}
        await self.channel.emit("question", status="resolved", answers=answers, **base)
        await self.channel.set_state("running")
        return PermissionResultAllow(
            updated_input={**tool_input, "answers": answers_by_prompt(questions, answers)}
        )

    def _cancel_pending(self, reason: str) -> None:
        """Resolve every outstanding approval and question as a refusal."""
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result({"option_id": "deny", "message": reason, "answers": None})
            self._pending.pop(request_id, None)

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result({"option_id": option_id, "message": message})
        return True

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result({"answers": answers})
        return True
