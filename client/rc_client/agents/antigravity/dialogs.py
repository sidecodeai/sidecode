"""Everything `agy` asks a person in one session: approvals and forms (A64, 5.7, 5.8).

`agy` asks before a tool runs with `session/request_permission`, offering allow once and reject
once, and the device offers exactly those two; a refusal ends `agy`'s turn. It asks the model's own
questions with an `elicitation/create` form, raised as one `question` block. `agy` sends nothing to
close a request that Stop or the end of the turn left open, and ignores an answer that comes after,
so the device expires every card itself when the turn ends, whatever ended it. A read `agy` asks for
of a file the device wrote for this session's own message is allowed by the device and raises no
card (4.8, A49).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...attachments import is_session_file
from ...logging_setup import logger
from ...models import SessionState
from ...sessions.channel import SessionChannel
from . import forms
from .translate import new_tool

log = logger("rc_client.antigravity.dialogs")

PERMISSION = "session/request_permission"
FORM = "elicitation/create"
CANCELLED_APPROVAL: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
CANCELLED_FORM: dict[str, Any] = {"action": "cancel"}
# The two kinds of option the device offers, and how each reads in an app.
STYLES = {"allow_once": "primary", "reject_once": "danger"}
# What `agy`'s own `read_file` carries in `rawInput` besides the path; anything else is asked about.
READ_KEYS = frozenset({"Path", "AbsolutePath", "StartLine", "EndLine", "toolSummary"})
READ_PATH_KEYS = ("Path", "AbsolutePath")
WAITING = frozenset({"needs_approval", "needs_input"})

Respond = Callable[[Any, dict[str, Any]], Awaitable[None]]
RespondError = Callable[[Any, int, str], Awaitable[None]]


@dataclass(slots=True)
class _Card:
    """One open card: the request `agy` waits on, what the apps see, and how it ends."""

    agy_id: Any
    kind: str
    block: dict[str, Any]
    cancel: dict[str, Any]
    form: forms.Form | None = None
    options: set[str] = field(default_factory=set)
    future: asyncio.Future[dict[str, Any]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class Dialogs:
    """The open approvals and questions of one Antigravity session."""

    def __init__(
        self, channel: SessionChannel, respond: Respond, respond_error: RespondError
    ) -> None:
        self._channel = channel
        self._respond = respond
        self._respond_error = respond_error
        self._cards: dict[str, _Card] = {}
        self._waits: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> bool:
        """Whether a card of the turn waits for an answer, which `agy` takes no steer during."""
        return bool(self._cards)

    # --------------------------------------------------------------- inbound

    async def request(self, agy_id: Any, method: str, params: dict[str, Any]) -> None:
        """One of `agy`'s requests: its card goes up now, and a wait of its own answers it."""
        if method == PERMISSION:
            await self._permission(agy_id, params)
        elif method == FORM:
            await self._form(agy_id, params)
        else:
            await self._respond_error(agy_id, -32601, f"unhandled request {method}")

    async def _permission(self, agy_id: Any, params: dict[str, Any]) -> None:
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        offered = _options(params.get("options"))
        allow = next((option["id"] for option in offered if option["style"] == "primary"), None)
        if allow is not None and self._reads_own_file(call):
            await self._respond(agy_id, {"outcome": {"outcome": "selected", "optionId": allow}})
            return
        if not offered:
            log.warning("agy asked for an approval with no option the device offers")
            await self._respond(agy_id, CANCELLED_APPROVAL)
            return
        tool = new_tool(call)
        card_id = str(uuid.uuid4())
        block: dict[str, Any] = {
            "block_id": f"approval:{card_id}",
            "request_id": card_id,
            "tool": tool.tool,
            "tool_kind": tool.tool_kind,
            "title": tool.title or tool.tool,
            "options": offered,
        }
        if tool.input:
            block["input"] = tool.input
        if tool.diff:
            block["diff"] = tool.diff
        card = _Card(agy_id, "approval", block, CANCELLED_APPROVAL)
        card.options = {option["id"] for option in offered}
        await self._open(card, "needs_approval")

    async def _form(self, agy_id: Any, params: dict[str, Any]) -> None:
        try:
            form = forms.read(params)
        except ValueError:
            log.warning("agy asked a question the device cannot show")
            await self._respond(agy_id, CANCELLED_FORM)
            return
        card_id = str(uuid.uuid4())
        block = {
            "block_id": f"question:{card_id}",
            "request_id": card_id,
            "questions": form.block(),
        }
        await self._open(_Card(agy_id, "question", block, CANCELLED_FORM, form=form), "needs_input")

    async def _open(self, card: _Card, state: SessionState) -> None:
        self._cards[card.block["request_id"]] = card
        await self._channel.emit(card.kind, status="pending", **card.block)
        await self._channel.set_state(state)
        wait = asyncio.create_task(self._settle(card), name="agy-card")
        self._waits.add(wait)
        wait.add_done_callback(self._waits.discard)

    # -------------------------------------------------------------- answers

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        """An app's answer to an approval; a `message` has nowhere to go in `agy` (5.7)."""
        card = self._cards.get(request_id)
        if card is None or card.kind != "approval" or card.future.done():
            return False
        card.future.set_result({"option_id": option_id})
        return True

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        card = self._cards.get(request_id)
        if card is None or card.kind != "question" or card.future.done():
            return False
        card.future.set_result({"answers": answers})
        return True

    async def _settle(self, card: _Card) -> None:
        try:
            outcome = await card.future
        except asyncio.CancelledError:
            return
        if self._cards.get(card.block["request_id"]) is not card:
            return
        del self._cards[card.block["request_id"]]
        reply = self._reply(card, outcome)
        if reply is None:
            await self._channel.emit(card.kind, status="expired", **card.block)
            if not outcome.get("let_go"):
                await self._respond(card.agy_id, card.cancel)
            return
        answer, decision = reply
        await self._respond(card.agy_id, answer)
        await self._channel.emit(card.kind, status="resolved", **decision, **card.block)
        await self._back_to_running()

    def _reply(
        self, card: _Card, outcome: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """What answers `agy` and what the resolved card says, or None for a card that expires."""
        if card.kind == "approval":
            option_id = str(outcome.get("option_id") or "")
            if option_id not in card.options:
                if option_id:
                    log.warning("an approval answered with an option the device never offered")
                return None
            return (
                {"outcome": {"outcome": "selected", "optionId": option_id}},
                {"decision": {"option_id": option_id, "by": "remote"}},
            )
        answers = outcome.get("answers")
        if card.form is None or not isinstance(answers, dict):
            return None
        return forms.accepted(card.form, answers), {"answers": answers, "by": "remote"}

    async def _back_to_running(self) -> None:
        """The turn goes on, unless another card still waits or the turn is over."""
        session = self._channel.session
        if session.turn is None or session.state not in WAITING:
            return
        remaining = {card.kind for card in self._cards.values()}
        if "approval" in remaining:
            await self._channel.set_state("needs_approval")
        elif "question" in remaining:
            await self._channel.set_state("needs_input")
        else:
            await self._channel.set_state("running")

    # ------------------------------------------------------------- endings

    async def expire_all(self, *, answer_agy: bool = True) -> None:
        """Expire every open card: Stop, or the turn's end. `agy` is told so unless it is going."""
        cards = list(self._cards.values())
        for card in cards:
            if not card.future.done():
                card.future.set_result({} if answer_agy else {"let_go": True})
        if self._waits:
            await asyncio.gather(*list(self._waits), return_exceptions=True)

    async def let_go(self) -> None:
        """The process is going: every card expires, and nothing is answered."""
        await self.expire_all(answer_agy=False)
        for wait in list(self._waits):
            wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wait

    def _reads_own_file(self, call: dict[str, Any]) -> bool:
        """Whether the call is `agy`'s own `read_file` of this session's files alone (A49)."""
        if call.get("kind") not in (None, "read") or call.get("name") not in (None, "read_file"):
            return False
        raw = call.get("rawInput")
        if not isinstance(raw, dict) or not set(raw) <= READ_KEYS:
            return False
        locations = call.get("locations")
        paths: list[Any] = [raw[key] for key in READ_PATH_KEYS if key in raw]
        for place in locations if isinstance(locations, list) else []:
            paths.append(place.get("path") if isinstance(place, dict) else None)
        session_id = self._channel.session.session_id
        return bool(paths) and all(is_session_file(session_id, path) for path in paths)


def _options(options: Any) -> list[dict[str, str]]:
    """`agy`'s allow-once and reject-once options; any other kind it lists is not offered."""
    styled: list[dict[str, str]] = []
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        style = STYLES.get(str(option.get("kind") or ""))
        option_id = str(option.get("optionId") or "")
        if style is None or not option_id:
            continue
        styled.append(
            {"id": option_id, "label": str(option.get("name") or option_id), "style": style}
        )
    styles = {option["style"] for option in styled}
    return styled if styles == {"primary", "danger"} else []
