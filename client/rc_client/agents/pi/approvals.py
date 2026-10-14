"""The tool calls a pi session is holding, as protocol `approval` blocks.

pi has no permission system of its own, so these questions are the device's:
the extension stops a tool call, asks here, and runs or refuses it by what an
app answers (A26 4.3). The waiting lives inside the pi process — this side only
publishes the block and posts the decision back down the socket — which is why
nothing here holds a future.

In a terminal session the same question is also on screen, and whichever side
answers first wins (A20). A question the terminal took is resolved here as
`elsewhere`, the way an approval a Codex terminal answered first is.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...errors import RcError
from ...sessions.channel import SessionChannel
from .translate import tool_kind, tool_title

ALLOW = "allow"
ALLOW_SESSION = "allow_session"
DENY = "deny"
# Never offered, only reported: how a block resolves when the terminal answered.
ELSEWHERE = "elsewhere"

OPTIONS = [
    {"id": ALLOW, "label": "Allow", "style": "primary"},
    {"id": ALLOW_SESSION, "label": "Allow for this session", "style": "secondary"},
    {"id": DENY, "label": "Deny", "style": "danger"},
]
DECISIONS = frozenset({ALLOW, ALLOW_SESSION, DENY})

Answer = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class _Open:
    """One question the extension is holding a tool call for."""

    ask_id: str
    request_id: str
    block_id: str
    tool: str
    title: str
    input: dict[str, Any]


class PiApprovals:
    """Publishes one session's approval blocks and routes the answers back."""

    def __init__(self, channel: SessionChannel, answer: Answer) -> None:
        self._channel = channel
        self._answer = answer
        self._open: dict[str, _Open] = {}
        self._by_ask: dict[str, str] = {}

    @property
    def waiting(self) -> bool:
        return bool(self._open)

    async def ask(self, frame: dict[str, Any]) -> None:
        """An `ask` frame: raise the question the tool call is stopped on."""
        ask_id = str(frame.get("id") or "")
        if not ask_id or ask_id in self._by_ask:
            return
        tool = str(frame.get("tool") or "tool")
        arguments = frame.get("input")
        arguments = arguments if isinstance(arguments, dict) else {}
        question = _Open(
            ask_id=ask_id,
            request_id=str(uuid.uuid4()),
            block_id=f"approval:{uuid.uuid4()}",
            tool=tool,
            title=tool_title(arguments, tool),
            input=arguments,
        )
        self._open[question.request_id] = question
        self._by_ask[ask_id] = question.request_id
        await self._emit(question, status="pending")

    async def approve(self, request_id: str, option_id: str) -> bool:
        """An app answered. False when the question is no longer open."""
        if option_id not in DECISIONS:
            raise RcError("bad_request", f"this approval does not offer {option_id}")
        question = self._open.get(request_id)
        if question is None:
            return False
        self._forget(question)
        await self._answer(question.ask_id, option_id)
        await self._emit(
            question, status="resolved", decision={"option_id": option_id, "by": "remote"}
        )
        return True

    async def closed(self, frame: dict[str, Any]) -> None:
        """An `ask_closed` frame: the terminal's own dialog answered first."""
        request_id = self._by_ask.get(str(frame.get("id") or ""))
        question = self._open.get(request_id or "")
        if question is None:
            return
        self._forget(question)
        await self._emit(
            question, status="resolved", decision={"option_id": ELSEWHERE, "by": "terminal"}
        )

    async def expire(self) -> None:
        """pi left, so nobody is holding these tool calls any more."""
        for question in list(self._open.values()):
            self._forget(question)
            await self._emit(question, status="expired")

    def _forget(self, question: _Open) -> None:
        self._open.pop(question.request_id, None)
        self._by_ask.pop(question.ask_id, None)

    async def _emit(
        self, question: _Open, *, status: str, decision: dict[str, str] | None = None
    ) -> None:
        fields: dict[str, Any] = {
            "block_id": question.block_id,
            "request_id": question.request_id,
            "tool": question.tool,
            "tool_kind": tool_kind(question.tool),
            "title": question.title,
            "input": dict(question.input),
            "options": [dict(option) for option in OPTIONS],
            "status": status,
        }
        if decision is not None:
            fields["decision"] = decision
        await self._channel.emit("approval", **fields)
