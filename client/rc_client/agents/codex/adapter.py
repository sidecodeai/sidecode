"""Drive one Codex session through a private `codex app-server` process."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ...attachments import Attachment, describe, materialise, wire_attachments
from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting, now_ms
from ...sessions.channel import SessionChannel
from ...sessions.limits import LimitStop
from ..base import Emit
from . import commands as slash
from .echoes import Echo, EchoLog
from .models import ModelCatalog, tier_id
from .prompts import answers_payload, question_blocks
from .reports import ThreadFacts
from .rpc import CodexAppServer
from .translate import CodexTranslator, item_type, text_of

log = logger("rc_client.codex")

APPROVAL_TIMEOUT = 300.0
OUTPUT_THROTTLE = 0.2
DRAIN_TIMEOUT = 15.0

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
}
_COMMAND_OPTIONS = [
    {"id": "accept", "label": "Approve", "style": "primary"},
    {"id": "acceptForSession", "label": "Approve for this session", "style": "secondary"},
    {"id": "decline", "label": "Decline", "style": "danger"},
    {"id": "cancel", "label": "Stop the turn", "style": "secondary"},
]
_PERMISSION_OPTIONS = [
    {"id": "accept", "label": "Grant for this turn", "style": "primary"},
    {"id": "acceptForSession", "label": "Grant for this session", "style": "secondary"},
    {"id": "decline", "label": "Decline", "style": "danger"},
]

TurnEndCallback = Callable[[], Awaitable[None]]


class CodexRunner:
    agent = "codex"

    def __init__(
        self,
        channel: SessionChannel,
        *,
        binary: str,
        cwd: str,
        catalog: ModelCatalog,
        model: str | None = None,
        permission_mode: str | None = None,
        effort: str | None = None,
        speed: str | None = None,
        thread_id: str | None = None,
        on_turn_end: TurnEndCallback | None = None,
        on_session_id: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.channel = channel
        self._binary = binary
        self._cwd = cwd
        self._catalog = catalog
        self._model = model
        self._permission_mode = permission_mode or "on-request"
        self._effort = effort
        self._speed = speed
        self._thread_id = thread_id
        self._on_turn_end = on_turn_end
        self._on_session_id = on_session_id
        self._server: CodexAppServer | None = None
        self._sandbox: dict[str, Any] | None = None
        self._translator = CodexTranslator(cwd=cwd, mirror_user_messages=False)
        self._turn_id: str | None = None
        self._turn_started_at = 0
        self._interrupting = False
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._last_output_flush: dict[str, float] = {}
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._turn_epoch = 0
        self._echoes = EchoLog()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        server = CodexAppServer(
            self._binary,
            on_notification=self._on_notification,
            on_request=self._on_request,
            env=sanitized_child_env(),
        )
        await server.start()
        self._server = server
        params: dict[str, Any] = {"cwd": self._cwd, "approvalPolicy": self._permission_mode}
        if self._model:
            params["model"] = self._model
        if self._thread_id:
            params["threadId"] = self._thread_id
            params["excludeTurns"] = True
            result = await server.request("thread/resume", params)
        else:
            result = await server.request("thread/start", params)
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or result.get("threadId") or "")
        if not thread_id:
            raise RcError("agent_unavailable", "codex did not return a thread id")
        self._thread_id = thread_id
        sandbox = result.get("sandbox")
        self._sandbox = sandbox if isinstance(sandbox, dict) else None
        wanted_speed = self._speed
        self._speed = tier_id(result.get("serviceTier"))
        if wanted_speed is not None and wanted_speed != self._speed:
            # `thread/start` accepts `serviceTier` and ignores it, so a session
            # running at a tier asks for it once the thread is up (A21).
            self._speed = wanted_speed
            await server.request(
                "thread/settings/update", {"threadId": thread_id, "serviceTier": wanted_speed}
            )
        await self.channel.set_meta(speed=self._speed)
        if self._model is None:
            model = result.get("model")
            if isinstance(model, str) and model:
                self._model = model
                await self.channel.set_meta(model=model)
        if self._on_session_id:
            await self._on_session_id(thread_id)

    async def close(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        if self._server is not None:
            await self._server.close()
            self._server = None

    async def shutdown(self) -> None:
        """A39: the app-server child is this device's own, so closing it ends the thread."""
        await self.close()

    # ------------------------------------------------------------- streaming

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            turn = params.get("turn") or {}
            self._turn_id = str(turn.get("id") or "") or self._turn_id
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict) and item_type(item) == "userMessage":
                await self._take_echo(item)
        completion: dict[str, Any] | None = None
        for emit in self._translator.notification(method, params):
            if emit.kind == "turn_completed":
                completion = dict(emit.fields)
                continue
            await self._apply(emit)
        if completion is not None:
            await self._finish_turn(completion)

    async def _take_echo(self, item: dict[str, Any]) -> None:
        """Publish the `user_message` a steer held back, if this is its echo.

        Codex echoes a steered prompt at the step that reads it, so this is
        where the bubble belongs (amendment A14). The echo is consumed by the
        first of the item's two events, which is what keeps the second silent.
        """
        claimed = self._echoes.claim("", text_of(item.get("content")), "")
        if claimed is not None:
            await self._publish_steer(claimed)

    async def _publish_steer(self, echo: Echo) -> None:
        if echo.block_id is None:
            return
        await self.channel.emit(
            "user_message", block_id=echo.block_id, text=echo.text, source="remote"
        )

    async def _publish_unread(self, stop_reason: str) -> None:
        """Show the steered messages this turn ended without ever reading."""
        for echo in self._echoes.unclaimed():
            await self._publish_steer(echo)
            if stop_reason == "interrupted":
                await self.channel.notice(
                    "warn", "the agent was stopped before it read your message"
                )

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

    async def _finish_turn(self, completion: dict[str, Any]) -> None:
        duration = int(completion.get("duration_ms") or 0) or max(
            0, now_ms() - self._turn_started_at
        )
        reported = str(completion.get("stop_reason") or "completed")
        stop_reason = "interrupted" if self._interrupting else reported
        self._interrupting = False
        await self._publish_unread(stop_reason)
        self._turn_id = None
        self._turn_epoch += 1
        self._turn_done.set()
        self._last_output_flush.clear()
        usage = dict(completion.get("usage") or self._translator.usage)
        # A private app-server has no quota read of its own (A33), so a usage
        # limit stop here says that and no more (amendment A35).
        limit = completion.get("limit")
        await self.channel.end_turn(
            stop_reason,
            duration,
            usage or None,
            limit=limit if isinstance(limit, LimitStop) else None,
        )
        if self._on_turn_end is not None:
            await self._on_turn_end()

    # --------------------------------------------------------------- driving

    @property
    def busy(self) -> bool:
        return self._turn_id is not None

    @property
    def supports_steer(self) -> bool:
        return True

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        prompt = text
        written: list[Attachment] = []
        if attachments:
            written = materialise(self.channel.session.session_id, attachments)
            prompt = describe(text, written)
        await self._deliver(text, prompt, written, source, block_id)

    async def run_prompt(self, prompt: str, shown: str, block_id: str) -> None:
        """Start a turn on a prompt the user never typed, such as `/init` (A27)."""
        await self._deliver(shown, prompt, [], "remote", block_id)

    async def _deliver(
        self,
        shown: str,
        prompt: str,
        written: list[Attachment],
        source: str,
        block_id: str | None,
    ) -> None:
        """Publish the bubble, then start the turn the prompt asks for.

        `shown` and `prompt` differ only for a slash command that stands for a
        canned prompt: the bubble keeps what the user typed.
        """
        server = self._server
        if server is None or self._thread_id is None:
            raise RcError("agent_unavailable", "the Codex session is not connected")
        await self.channel.emit(
            "user_message",
            block_id=block_id or f"user:{uuid.uuid4()}",
            text=shown,
            source=source,
            **({"attachments": wire_attachments(written)} if written else {}),
        )
        self._turn_started_at = now_ms()
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": prompt}],
            "approvalPolicy": self._permission_mode,
        }
        if self._model:
            params["model"] = self._model
        effort = self._catalog.clamp_effort(self._model, self._effort)
        if effort:
            params["effort"] = effort
        epoch = self._turn_epoch
        self._turn_done.clear()
        result = await server.request("turn/start", params)
        turn = result.get("turn") or {}
        if epoch != self._turn_epoch:
            # `turn/completed` was dispatched while `turn/start` was still in
            # flight; the turn is already over, so do not mark it live again.
            return
        self._turn_id = str(turn.get("id") or "") or str(uuid.uuid4())
        await self.channel.begin_turn(source)

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        server = self._server
        if server is None or self._thread_id is None or self._turn_id is None:
            return False
        try:
            await server.request(
                "turn/steer",
                {
                    "threadId": self._thread_id,
                    "expectedTurnId": self._turn_id,
                    "input": [{"type": "text", "text": text}],
                },
            )
        except RcError:
            return False
        # Amendment A14: the bubble waits for Codex's echo of this prompt, which
        # arrives at the step that reads it, not where it was sent.
        evicted = self._echoes.remember(text, block_id or f"user:{uuid.uuid4()}")
        if evicted is not None:
            await self._publish_steer(evicted)
        return True

    async def interrupt(self) -> bool:
        server = self._server
        if server is None or self._thread_id is None or self._turn_id is None:
            return False
        self._interrupting = True
        await self.channel.set_state("running", "interrupting")
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result({"option_id": "decline", "answers": None})
            self._pending.pop(request_id, None)
        try:
            await server.request(
                "turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id}
            )
        except RcError:
            log.warning("codex interrupt was rejected; waiting for the turn to settle")
        # Wait for the terminal notification under one absolute deadline, so a
        # follow-up `turn/start` cannot race the turn we just stopped.
        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=DRAIN_TIMEOUT)
        except TimeoutError:
            log.warning("codex did not settle after interrupt; restarting the app-server")
            await self.channel.notice("warn", "the agent did not stop in time; reconnecting")
            await self._restart()
        return True

    async def _restart(self) -> None:
        """Rebuild the app-server connection, resuming the same thread."""
        # The turn dies with the process, so no `turn/completed` is coming and
        # this is the last chance to show a steered message that was never read.
        await self._publish_unread("interrupted")
        thread_id = self._thread_id
        await self.close()
        self._thread_id = thread_id
        self._turn_id = None
        self._turn_epoch += 1
        self._turn_done.set()
        await self.start()

    # -------------------------------------------------------------- commands

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

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
        server = self._server
        if server is None:
            raise RcError("agent_unavailable", "the Codex session is not connected")
        return await server.request(method, params)

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
        if model is not None:
            self._model = model
        if permission_mode is not None:
            self._permission_mode = permission_mode
        if effort is not None:
            self._effort = effort
        if speed is UNSET:
            return
        self._speed = speed
        # Model, permission mode and effort ride on the next `turn/start`; the
        # tier is thread state, so it takes a request of its own (A21).
        server = self._server
        if server is not None and self._thread_id:
            await server.request(
                "thread/settings/update", {"threadId": self._thread_id, "serviceTier": speed}
            )

    # ------------------------------------------------------- prompts to user

    async def _on_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in APPROVAL_METHODS:
            return await self._handle_approval(method, params)
        if method == "item/tool/requestUserInput":
            return await self._handle_question(params)
        raise RcError("unsupported", f"unhandled codex request {method}")

    async def _wait_for(self, request_id: str) -> dict[str, Any] | None:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            if future.cancelled():
                return None
            raise
        finally:
            self._pending.pop(request_id, None)

    async def _handle_approval(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        permission = method.endswith("permissions/requestApproval")
        request_id = str(uuid.uuid4())
        block_id = f"approval:{request_id}"
        base: dict[str, Any] = {
            "block_id": block_id,
            "request_id": request_id,
            "tool": "Permissions"
            if permission
            else ("Shell" if "commandExecution" in method else "Apply patch"),
            "tool_kind": "other"
            if permission
            else ("shell" if "commandExecution" in method else "edit"),
            "title": self._approval_title(method, params),
            "input": {
                key: params[key]
                for key in ("command", "cwd", "reason", "grantRoot", "permissions")
                if key in params
            },
            "options": _PERMISSION_OPTIONS if permission else _COMMAND_OPTIONS,
        }
        # A patch approval carries no patch of its own: the change was streamed
        # as a fileChange item, so show that diff rather than an empty card.
        item_id = str(params.get("itemId") or "")
        if "fileChange" in method and item_id:
            diff = self._translator.pending_diff(item_id)
            if diff:
                base["diff"] = diff
                base["title"] = str(diff.get("path") or base["title"])
        await self.channel.emit("approval", status="pending", **base)
        await self.channel.set_state("needs_approval")
        decision = await self._wait_for(request_id)
        option_id = str(decision.get("option_id")) if decision else "decline"
        allowed = {option["id"] for option in base["options"]}
        if option_id not in allowed:
            # An id we never advertised must never be read as consent.
            log.warning("unknown approval option; declining", method=method)
            option_id = "decline"
        await self.channel.emit(
            "approval",
            status="resolved" if decision else "expired",
            **({"decision": {"option_id": option_id, "by": "remote"}} if decision else {}),
            **base,
        )
        await self.channel.set_state("running")
        if permission:
            accepted = option_id in {"accept", "acceptForSession"}
            granted = params.get("permissions") if accepted else {}
            return {
                "permissions": granted or {},
                "scope": "session" if option_id == "acceptForSession" else "turn",
            }
        return {"decision": option_id}

    def _approval_title(self, method: str, params: dict[str, Any]) -> str:
        if "commandExecution" in method:
            command = params.get("command")
            text = " ".join(command) if isinstance(command, list) else str(command or "")
            return text.splitlines()[0][:200] if text else "run a command"
        if "fileChange" in method:
            return str(params.get("grantRoot") or "apply a patch")
        return str(params.get("reason") or "additional permissions")

    async def _handle_question(self, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        questions = question_blocks(params)
        base = {
            "block_id": f"question:{request_id}",
            "request_id": request_id,
            "questions": questions,
        }
        await self.channel.emit("question", status="pending", **base)
        await self.channel.set_state("needs_input")
        reply = await self._wait_for(request_id)
        answers = (reply or {}).get("answers") or {}
        await self.channel.emit(
            "question",
            status="resolved" if reply else "expired",
            **({"answers": answers} if reply else {}),
            **base,
        )
        await self.channel.set_state("running")
        return {"answers": answers_payload(questions, answers)}

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
