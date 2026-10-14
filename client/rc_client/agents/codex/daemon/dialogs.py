"""Everything the daemon asks a person, for one thread.

Approvals and questions arrive as JSON-RPC *requests* that stay open until
somebody answers, and they fan out to every client subscribed to the thread, so
the terminal may answer one before we do. Keeping that waiting in its own object
keeps the turn stream in `session.py` free of it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from ....errors import RcError
from ....logging_setup import logger
from ....sessions.channel import SessionChannel
from ..prompts import answers_payload, question_blocks
from . import approvals

log = logger("rc_client.codex.daemon.dialogs")

APPROVAL_TIMEOUT = 300.0
QUESTION_METHOD = "item/tool/requestUserInput"

DiffLookup = Callable[[str], dict[str, Any] | None]
BusyCheck = Callable[[], bool]


def approval_block(method: str, params: dict[str, Any], cwd: str) -> dict[str, Any]:
    """The tool identity, title and `input` an approval card carries (A11 5.7)."""
    if method == approvals.COMMAND_METHOD:
        command = str(params.get("command") or "")
        return {
            "tool": "shell",
            "tool_kind": "shell",
            "title": (command.splitlines()[0][:200] if command else "run a command"),
            "input": {
                "command": command,
                "cwd": str(params.get("cwd") or cwd),
                "command_actions": list(params.get("commandActions") or []),
            },
        }
    if method == approvals.FILE_CHANGE_METHOD:
        return {
            "tool": "apply_patch",
            "tool_kind": "edit",
            "title": str(params.get("grantRoot") or "apply a patch"),
            "input": {
                "grant_root": str(params.get("grantRoot") or ""),
                "reason": str(params.get("reason") or ""),
            },
        }
    return {
        "tool": "permissions",
        "tool_kind": "other",
        "title": str(params.get("reason") or "additional permissions"),
        "input": {
            "reason": str(params.get("reason") or ""),
            "cwd": str(params.get("cwd") or cwd),
            "permissions": params.get("permissions") or {},
        },
    }


def fallback_decisions(method: str) -> tuple[str, ...]:
    """What a prompt offers when the daemon sends no `availableDecisions`."""
    if method == approvals.FILE_CHANGE_METHOD:
        return approvals.FILE_CHANGE_DECISIONS
    if method == approvals.PERMISSIONS_METHOD:
        return approvals.PERMISSION_DECISIONS
    return (approvals.ACCEPT, approvals.CANCEL)


class DialogDesk:
    """The open approvals and questions of one thread, and how they end."""

    def __init__(
        self,
        channel: SessionChannel,
        *,
        cwd: str,
        busy: BusyCheck,
        pending_diff: DiffLookup,
    ) -> None:
        self._channel = channel
        self._cwd = cwd
        self._busy = busy
        self._pending_diff = pending_diff
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._daemon_ids: dict[str, Any] = {}
        self._offers: dict[str, approvals.ApprovalOptions] = {}

    # ------------------------------------------------------------- inbound

    async def request(self, daemon_id: Any, method: str, params: dict[str, Any]) -> Any:
        if method in approvals.APPROVAL_METHODS:
            return await self._approval(daemon_id, method, params)
        if method == QUESTION_METHOD:
            return await self._question(daemon_id, params)
        raise RcError("unsupported", f"unhandled codex request {method}")

    async def resolved_elsewhere(self, daemon_id: Any) -> None:
        """`serverRequest/resolved`: somebody else answered this prompt first."""
        for request_id, known in list(self._daemon_ids.items()):
            if known != daemon_id:
                continue
            future = self._waiting.get(request_id)
            if future is not None and not future.done():
                future.set_result({"elsewhere": True})
            return

    # ------------------------------------------------------------ outbound

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        future = self._waiting.get(request_id)
        if future is None or future.done():
            return False
        offered = self._offers.get(request_id)
        if offered is not None and not offered.offers(option_id):
            # PROTOCOL 6.3: an id the block never offered, `elsewhere` included,
            # is a bad request rather than a quiet refusal of the tool call.
            raise RcError("bad_request", f"this approval does not offer {option_id}")
        future.set_result({"option_id": option_id, "message": message})
        return True

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        future = self._waiting.get(request_id)
        if future is None or future.done():
            return False
        future.set_result({"answers": answers})
        return True

    def deny_all(self) -> None:
        """Stop waiting on every prompt, refusing each: the turn is being stopped."""
        for request_id, future in list(self._waiting.items()):
            if not future.done():
                future.set_result({"option_id": approvals.DENY, "answers": None})
            self._waiting.pop(request_id, None)

    def cancel_all(self) -> None:
        for future in self._waiting.values():
            if not future.done():
                future.cancel()
        self._waiting.clear()
        self._daemon_ids.clear()
        self._offers.clear()

    # -------------------------------------------------------------- waiting

    async def _wait(self, request_id: str, daemon_id: Any) -> dict[str, Any] | None:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        self._daemon_ids[request_id] = daemon_id
        try:
            return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            if future.cancelled():
                return None
            raise
        finally:
            self._waiting.pop(request_id, None)
            self._daemon_ids.pop(request_id, None)
            self._offers.pop(request_id, None)

    async def _settle(self) -> None:
        await self._channel.set_state("running" if self._busy() else "idle")

    # ------------------------------------------------------------ approvals

    async def _approval(
        self, daemon_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        options = approvals.build_options(
            params.get("availableDecisions"), fallback_decisions(method)
        )
        base: dict[str, Any] = {
            "block_id": f"approval:{request_id}",
            "request_id": request_id,
            **approval_block(method, params, self._cwd),
            "options": options.wire(),
        }
        self._offers[request_id] = options
        item_id = str(params.get("itemId") or "")
        if method == approvals.FILE_CHANGE_METHOD and item_id:
            diff = self._pending_diff(item_id)
            if diff:
                base["diff"] = diff
                base["title"] = str(diff.get("path") or base["title"])
        await self._channel.emit("approval", status="pending", **base)
        await self._channel.set_state("needs_approval")
        outcome = await self._wait(request_id, daemon_id)
        if outcome is not None and outcome.get("elsewhere"):
            await self._resolve(base, approvals.ELSEWHERE, "terminal")
            # Our answer would be discarded silently anyway; say nothing useful.
            return {}
        option_id = str(outcome.get("option_id")) if outcome else approvals.DENY
        if not options.offers(option_id):
            log.warning("unknown codex approval option; denying", method=method)
            option_id = approvals.DENY
        await self._resolve(base, option_id, "remote", resolved=outcome is not None)
        if method == approvals.PERMISSIONS_METHOD:
            return approvals.permission_reply(option_id, params)
        return approvals.command_reply(options.decisions[option_id])

    async def _resolve(
        self, base: dict[str, Any], option_id: str, by: str, resolved: bool = True
    ) -> None:
        await self._channel.emit(
            "approval",
            status="resolved" if resolved else "expired",
            **({"decision": {"option_id": option_id, "by": by}} if resolved else {}),
            **base,
        )
        await self._settle()

    # ------------------------------------------------------------ questions

    async def _question(self, daemon_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        questions = question_blocks(params)
        base = {
            "block_id": f"question:{request_id}",
            "request_id": request_id,
            "questions": questions,
        }
        await self._channel.emit("question", status="pending", **base)
        await self._channel.set_state("needs_input")
        reply = await self._wait(request_id, daemon_id)
        if reply is not None and reply.get("elsewhere"):
            await self._channel.emit("question", status="expired", **base)
            await self._settle()
            return {}
        answers = (reply or {}).get("answers") or {}
        await self._channel.emit(
            "question",
            status="resolved" if reply else "expired",
            **({"answers": answers} if reply else {}),
            **base,
        )
        await self._settle()
        return {"answers": answers_payload(questions, answers)}
