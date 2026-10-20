"""Everything Grok asks a person, for one session (A28, A52).

Grok asks through agent-to-client requests that stay open until somebody
answers: `session/request_permission` before a tool runs, and
`_x.ai/ask_user_question` when the model has a question. On a private child the
device is the only one asked. On the leader every client of the session is asked
at once, and the first reply settles it for all of them — a refusal or a
JSON-RPC error just as much as an answer. So while somebody else can answer, the
device never sends a reply it does not mean as one: it says nothing and leaves
the prompt to them. An approval somebody else answered ends with
`interaction_resolved`; a question ends with its tool's own result, which is the
only signal that belongs to the question alone: the permission check before it
resolves under the same tool call id.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...attachments import is_session_file
from ...errors import RcError
from ...logging_setup import logger
from ...sessions.channel import SessionChannel
from .acp import INTERACTIONS, PERMISSION_REQUEST, QUESTION_REQUEST
from .questions import DECLINED, Asked, accepted, answered_elsewhere, asked, ended

log = logger("rc_client.grok.dialogs")

# How long an approval only this device can answer waits before it is refused.
APPROVAL_TIMEOUT = 300.0
# The option that turns the whole session into always-approve mode. It is a
# permission policy, so it belongs to `session.set` and never to an approval
# button an app draws on the person's behalf (A27, A28).
ALWAYS_APPROVE = "enable-always-approve"
# What an approval that somebody else answered resolves as (A28, as for Codex).
ELSEWHERE = "elsewhere"
# The one option an attachment read is answered with: this call, nothing more.
ALLOW_ONCE = "allow-once"
# What Grok's own `read_file` may carry in `rawInput`: where the file is, how
# much of it to read, and the variant it names itself by. A key outside these
# is something this device does not understand, and is asked about.
READ_PATH_KEYS = ("target_file", "file_path", "path")
READ_OTHER_KEYS = ("variant", "pages", "format", "offset", "limit")
CANCELLED: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
# How a wait ended when nobody here answered.
CLOSED = "closed"
TIMED_OUT = "timed_out"
STOPPED = "cancelled"
SUPERSEDED = "superseded"
TURN_OVER = "turn_over"
# The states a prompt puts a session in, which its ending takes back to running.
WAITING = frozenset({"needs_approval", "needs_input"})

# ACP option kinds, and how firmly each reads in an app.
_OPTION_STYLES = {
    "allow_once": "primary",
    "allow_always": "secondary",
    "reject_once": "danger",
    "reject_always": "secondary",
}

SharedCheck = Callable[[], bool]
Pending = dict[str, asyncio.Future[dict[str, Any]]]


@dataclass(slots=True)
class _Question:
    """One question card, from the request that raised it to whatever ends it."""

    request_id: str
    tool_call_id: str
    asked: Asked

    @property
    def key(self) -> str:
        return self.tool_call_id or self.request_id

    def block(self) -> dict[str, Any]:
        return {
            "block_id": f"question:{self.request_id}",
            "request_id": self.request_id,
            "questions": self.asked.blocks,
        }


class DialogDesk:
    """The open approvals and questions of one Grok session, and how they end."""

    def __init__(self, channel: SessionChannel, *, shared: SharedCheck) -> None:
        self._channel = channel
        # Whether another client of the leader is in the session to answer.
        self._shared = shared
        self._approvals: Pending = {}
        self._answers: Pending = {}
        # The approval each tool call opened, so `interaction_resolved` finds it.
        self._tool_calls: dict[str, str] = {}
        # Every question card still open, by the tool call that asked it.
        self._questions: dict[str, _Question] = {}
        self._closing = False
        self._keep_for_others = False

    def open(self) -> None:
        """The runner started (again): a later close is a new one."""
        self._closing = False
        self._keep_for_others = False

    # --------------------------------------------------------------- inbound

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one agent-to-client request; `None` leaves it unanswered."""
        if method == PERMISSION_REQUEST:
            return await self._permission(params)
        if method == QUESTION_REQUEST:
            return await self._question(params)
        return self._unanswerable(method)

    def _unanswerable(self, method: str) -> dict[str, Any] | None:
        """A request this device cannot answer: another client may, else it is refused."""
        if method in INTERACTIONS and self._shared():
            log.info("left a grok prompt to the other clients", method=method)
            return None
        raise RcError("unsupported", f"unhandled grok request {method}")

    async def resolved_elsewhere(self, tool_call_id: str) -> None:
        """`interaction_resolved`: somebody else answered an approval this device shows."""
        request_id = self._tool_calls.get(tool_call_id)
        future = self._approvals.get(request_id) if request_id else None
        if future is not None and not future.done():
            future.set_result({ELSEWHERE: True})

    async def tool_result(self, update: dict[str, Any]) -> None:
        """A question's tool ended: settle the card nobody here answered.

        Whoever answered, and whatever the answer was, the question is over: a
        wait still open for it is let go without a reply.
        """
        if not ended(update):
            return
        card = self._questions.get(str(update.get("toolCallId") or ""))
        if card is None:
            return
        self._release(self._answers.get(card.request_id), ELSEWHERE)
        chosen = answered_elsewhere(card.asked, update)
        if chosen is None:
            await self._settle(card, "expired")
        else:
            # Answered at the terminal, or by another app on the leader (A20, A52).
            fields: dict[str, Any] = {"answers": chosen} if chosen else {}
            await self._settle(card, "resolved", by="terminal", **fields)
        await self._resume()

    # -------------------------------------------------------------- outbound

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        future = self._approvals.get(request_id)
        if future is None or future.done():
            return False
        future.set_result({"option_id": option_id, "message": message})
        return True

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        future = self._answers.get(request_id)
        if future is None or future.done():
            return False
        future.set_result({"answers": answers})
        return True

    def stop_all(self) -> None:
        """The turn is being stopped: every open prompt is refused, for everyone."""
        for pending in (self._approvals, self._answers):
            for future in pending.values():
                self._release(future, STOPPED)
            pending.clear()

    async def let_go(self, *, keep_for_others: bool) -> None:
        """The runner is closing; on a session a terminal shares, what is open stays theirs."""
        self._closing = True
        self._keep_for_others = keep_for_others
        for pending in (self._approvals, self._answers):
            for future in pending.values():
                if not future.done():
                    future.cancel()
            pending.clear()
        self._tool_calls.clear()
        for card in list(self._questions.values()):
            await self._settle(card, "expired")

    async def turn_over(self) -> None:
        """The turn ended, and nothing it asked can be answered any more: all of it expires."""
        for pending in (self._approvals, self._answers):
            for future in pending.values():
                self._release(future, TURN_OVER)
        for card in list(self._questions.values()):
            await self._settle(card, "expired")
        if self._channel.session.turn is None and self._channel.session.state in WAITING:
            # A turn this device never saw begin has no end of its own to put the
            # session back to idle; one it did see ends with `end_turn` next.
            await self._channel.set_state("idle")

    # --------------------------------------------------------------- waiting

    def _expect(self, pending: Pending, request_id: str) -> asyncio.Future[dict[str, Any]]:
        """Start waiting before anything is published, so no answer can slip past."""
        self._release(pending.get(request_id), SUPERSEDED)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending[request_id] = future
        return future

    @staticmethod
    def _release(future: asyncio.Future[dict[str, Any]] | None, how: str) -> None:
        if future is not None and not future.done():
            future.set_result({how: True})

    async def _wait(
        self,
        pending: Pending,
        request_id: str,
        future: asyncio.Future[dict[str, Any]],
        timeout: float | None,
        *,
        survive_teardown: bool = False,
    ) -> dict[str, Any]:
        """The reply, or how the wait ended without one.

        A wait the connection's own teardown cancels is a close like any other,
        unless `survive_teardown` asks to keep it: the leader asks again whoever
        joins while a prompt is open, the device after a reconnect included.
        """
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            return {TIMED_OUT: True}
        except asyncio.CancelledError:
            if not future.cancelled() or (survive_teardown and not self._closing):
                raise
            return {CLOSED: True}
        finally:
            if pending.get(request_id) is future:
                pending.pop(request_id, None)

    def _unanswered(
        self, outcome: dict[str, Any], *, closed: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The reply to a prompt nobody here answered: none, while another client can."""
        if outcome.get(TURN_OVER) or outcome.get(SUPERSEDED):
            return None
        if outcome.get(CLOSED):
            return None if self._keep_for_others else closed
        if outcome.get(TIMED_OUT) and self._shared():
            return None
        return closed

    async def _resume(self) -> None:
        """Back to running, unless the turn has moved on: a late ending must not revive it."""
        if self._channel.session.state in WAITING:
            await self._channel.set_state("running")

    # ------------------------------------------------------------- approvals

    async def _permission(self, params: dict[str, Any]) -> dict[str, Any] | None:
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        allow = _allow_once(params.get("options"))
        if allow and self._reads_session_file(call):
            # A49: an attachment the prompt named, which is the session's own.
            return {"outcome": {"outcome": "selected", "optionId": allow}}
        request_id = str(uuid.uuid4())
        options = _approval_options(params.get("options"))
        tool_call_id = str(call.get("toolCallId") or "")
        future = self._expect(self._approvals, request_id)
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
        # Only an approval nobody else can answer has a deadline of the device's
        # own; on a session a terminal shares it waits for them, or the turn's end.
        timeout = None if self._shared() else APPROVAL_TIMEOUT
        try:
            await self._channel.emit("approval", status="pending", **base)
            await self._channel.set_state("needs_approval")
            outcome = await self._wait(self._approvals, request_id, future, timeout)
        finally:
            if self._tool_calls.get(tool_call_id) == request_id:
                self._tool_calls.pop(tool_call_id, None)
        if outcome.get(ELSEWHERE):
            # Another client of the leader answered; the turn is already moving.
            await self._channel.emit(
                "approval",
                status="resolved",
                decision={"option_id": ELSEWHERE, "by": "terminal"},
                **base,
            )
            await self._resume()
            return CANCELLED
        allowed = {option["id"] for option in options}
        option_id = str(outcome.get("option_id") or "")
        if option_id not in allowed:
            # An id we never advertised must never be read as consent.
            if option_id:
                log.warning("unknown grok approval option; cancelling")
            await self._channel.emit("approval", status="expired", **base)
            if not outcome.get(TURN_OVER):
                await self._resume()
            return self._unanswered(outcome, closed=CANCELLED)
        await self._channel.emit(
            "approval", status="resolved", decision={"option_id": option_id, "by": "remote"}, **base
        )
        await self._resume()
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    def _reads_session_file(self, call: dict[str, Any]) -> bool:
        """Whether the call is Grok's own `read_file` of this session's files alone (A49).

        Every path the call names, in `rawInput` and in `locations`, has to be
        one of them; another tool, an unknown key or one path elsewhere means
        the request is asked about as any other.
        """
        raw = call.get("rawInput")
        named = _tool_input(call)
        if not _is_read_file(call) or not isinstance(raw, dict) or named is None:
            return False
        known = {*READ_PATH_KEYS, *READ_OTHER_KEYS}
        if not set(raw) <= known or not set(named) <= known:
            return False
        locations = call.get("locations") or []
        paths = [given[key] for given in (raw, named) for key in READ_PATH_KEYS if key in given]
        paths += [place.get("path") if isinstance(place, dict) else None for place in locations]
        session_id = self._channel.session.session_id
        return bool(paths) and all(is_session_file(session_id, path) for path in paths)

    # ------------------------------------------------------------- questions

    async def _question(self, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            question = asked(params)
        except ValueError:
            log.warning("an unusable grok question")
            return self._unanswerable(QUESTION_REQUEST)
        tool_call_id = str(params.get("toolCallId") or "")
        card = self._questions.get(tool_call_id) if tool_call_id else None
        asked_again = card is not None
        if card is None:
            card = _Question(str(uuid.uuid4()), tool_call_id, question)
            self._questions[card.key] = card
        future = self._expect(self._answers, card.request_id)
        if not asked_again:
            await self._channel.emit("question", status="pending", **card.block())
        await self._channel.set_state("needs_input")
        # No deadline of the device's own: Grok times a question out itself, and
        # its tool's result says so like any other ending.
        outcome = await self._wait(
            self._answers, card.request_id, future, None, survive_teardown=True
        )
        if outcome.get(ELSEWHERE):
            # Settled by the tool's own result; nothing sent now could change it.
            return None
        answers = outcome.get("answers")
        if isinstance(answers, dict):
            await self._settle(card, "resolved", answers=answers, by="remote")
            await self._resume()
            return accepted(card.asked, answers)
        if outcome.get(SUPERSEDED):
            # The leader asked again: the newer wait carries the card from here.
            return None
        await self._settle(card, "expired")
        if not outcome.get(CLOSED) and not outcome.get(TURN_OVER):
            await self._resume()
        return self._unanswered(outcome, closed=DECLINED)

    async def _settle(self, card: _Question, status: str, **fields: Any) -> None:
        """End one card, once: whichever path gets there first publishes it."""
        if self._questions.get(card.key) is not card:
            return
        self._questions.pop(card.key, None)
        await self._channel.emit("question", status=status, **fields, **card.block())


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


def _is_read_file(call: dict[str, Any]) -> bool:
    """Grok's own `read_file`, by the name `_meta` gives it or else by its variant."""
    if call.get("kind") not in (None, "read"):
        return False
    meta = call.get("_meta")
    tool = meta.get("x.ai/tool") if isinstance(meta, dict) else None
    raw = call.get("rawInput")
    variant = raw.get("variant") if isinstance(raw, dict) else None
    if tool is None:
        return variant == "ReadFile"
    builtin = isinstance(tool, dict) and tool.get("namespace") == "grok_build"
    return builtin and tool.get("name") == "read_file" and variant in (None, "ReadFile")


def _tool_input(call: dict[str, Any]) -> dict[str, Any] | None:
    """The input `_meta["x.ai/tool"]` repeats, empty when absent and `None` when unreadable."""
    meta = call.get("_meta")
    tool = meta.get("x.ai/tool") if isinstance(meta, dict) else None
    named = tool.get("input", {}) if isinstance(tool, dict) else {}
    return named if isinstance(named, dict) else None


def _allow_once(options: Any) -> str | None:
    """Grok's `allow-once`, if offered; never another option of the same kind.

    `enable-always-approve` calls itself `allow_once` too, so the kind is not
    enough to tell the two apart.
    """
    for option in options or []:
        if isinstance(option, dict) and (option.get("optionId") or option.get("id")) == ALLOW_ONCE:
            return ALLOW_ONCE
    return None


def _permission_title(call: dict[str, Any]) -> str:
    raw = call.get("rawInput")
    if isinstance(raw, dict):
        for key in ("command", "file_path", "path", "query"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().splitlines()[0][:200]
    title = str(call.get("title") or "")
    return title.splitlines()[0][:200] if title else "run a tool"
