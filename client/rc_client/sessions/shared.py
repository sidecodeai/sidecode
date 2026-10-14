"""Amendment A10: driving a terminal session the device is attached to.

The one rule the whole file exists for: a message injected while a turn is
running is reframed by the CLI as untrusted external data the model is told not
to obey, so the device injects only at an idle point and holds everything else
in its own queue. A held message is a queue entry and nothing else until it is
injected (amendment A19): the block appears where the terminal shows it, at the
end of the turn it waited for. Turn state comes from the transcript, which lags
reality by up to one tail interval, so an injection also marks itself in flight
until its own row appears.

A question the CLI asks is the one thing here that both sides can answer at the
same moment (amendment A20). The terminal's dialog and the block raised from the
`PermissionRequest` hook are one question; whichever answers first wins, and the
other side is told what happened.

A Claude channel carries user text and nothing else, so a settings change and
`/compact` are typed into the terminal instead, through the pseudo-terminal the
shim started the CLI inside (amendment A40). The scripts live in `typist.py`;
what is here is the claim on the transcript rows they leave behind, which is
the only thing that confirms a change actually happened. Stopping a turn takes
the same route since amendment A42: Escape, the key the person would press.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..agents.claude.questions import (
    answers_for_tool,
    answers_from_tool,
    normalise_questions,
)
from ..agents.claude.tools import tool_kind, tool_title
from ..errors import RcError
from ..logging_setup import logger
from ..models import now_ms
from . import titles
from .attach import Attachment, HookQuestion
from .limits import TurnEnd
from .ptys import PtyLink
from .queue import hold
from .typist import COMPACT_COMMAND, TypedCommand, Typist

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry, SessionHub

log = logger("rc_client.shared")

# How long an injection may stay unconfirmed before the next one is allowed.
INFLIGHT_TIMEOUT = 30.0
# How long to wait after a bridge disconnects before deciding whether the CLI
# process is still there: `/exit` closes the channel about a second early.
EXIT_SETTLE = 1.0
MAX_REMEMBERED = 32

ALLOW = "allow"
DENY = "deny"
BEHAVIORS = (ALLOW, DENY)

# Session-scoped grants are not available through the relay, so the two options
# below are the whole vocabulary of a shared approval.
APPROVAL_OPTIONS = [
    {"id": ALLOW, "label": "Allow", "style": "primary"},
    {"id": DENY, "label": "Deny", "style": "danger"},
]


@dataclass(slots=True)
class SharedApproval:
    request_id: str
    channel_id: str
    block_id: str
    tool: str
    title: str
    input: dict[str, Any]


@dataclass(slots=True)
class SharedQuestion:
    """A question the CLI is asking, as both a block and a waiting hook."""

    request_id: str
    block_id: str
    questions: list[dict[str, Any]]
    hook: HookQuestion


@dataclass(slots=True)
class SharedState:
    """Everything the device tracks about one attached CLI session."""

    attachment: Attachment
    running: bool = False
    inflight: str | None = None
    inflight_at: float = 0.0
    trigger: str = "terminal"
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: dict[str, SharedApproval] = field(default_factory=dict)
    by_channel: dict[str, str] = field(default_factory=dict)
    # One question at a time: the CLI asks the person one thing and waits.
    question: SharedQuestion | None = None
    # The command the device is typing into the terminal right now (A40).
    typed: TypedCommand | None = None

    def remember(self, item: dict[str, Any]) -> None:
        self.sent[str(item["message_id"])] = item
        for key in list(self.sent)[:-MAX_REMEMBERED]:
            self.sent.pop(key, None)

    def mark_inflight(self, message_id: str) -> None:
        self.inflight = message_id
        self.inflight_at = time.monotonic()

    @property
    def waiting(self) -> bool:
        """True while an injection has not yet shown up in the transcript."""
        if self.inflight is None:
            return False
        return (time.monotonic() - self.inflight_at) < INFLIGHT_TIMEOUT

    @property
    def busy(self) -> bool:
        """Whether a turn is under way, as the apps are told it is.

        An injection counts before its row reaches the transcript: the CLI has
        the message, so the turn has started even though the tailer has not
        caught up. What the apps see running is what Stop presses Escape on.
        """
        return self.running or self.waiting

    @property
    def busy_typing(self) -> bool:
        """Whether a command the device typed is still running (A40)."""
        return self.typed is not None and not self.typed.expired

    @property
    def injectable(self) -> bool:
        """Whether a message can go into the CLI right now.

        A question on screen is not an idle CLI: its dialog owns the prompt, and
        an injection would be read as an answer to it. Neither is a terminal the
        device is in the middle of typing a command into (A40).
        """
        return not self.busy and self.question is None and not self.busy_typing


def pending_item(text: str, request_id: str, source: str = "remote") -> dict[str, Any]:
    """One held message: a queue entry that also carries its bubble's identity.

    The bubble is the app's request id (amendment A12), which is also the id
    the queue reports, so a message drawn on sending and shown again as queued
    and once more as delivered is one block throughout. `message_id` stays the
    channel's own: it names the injection, not the bubble. `source` is what the
    bubble says the message was: a person's, or the device's resume (A35).
    """
    message_id = str(uuid.uuid4())
    block_id = request_id or str(uuid.uuid4())
    return {
        "id": block_id,
        "text": text,
        "ts": now_ms(),
        "attachments": [],
        "message_id": message_id,
        "block_id": block_id,
        "retried": False,
        "source": source,
    }


def approval_title(tool: str, preview: str, description: str, cwd: str) -> str:
    """Name the call from the relay's preview, falling back to its description.

    The preview is usually the tool input as JSON, which the ordinary tool
    titles understand; when it is plain text it is already the best summary
    there is.
    """
    try:
        parsed = json.loads(preview)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return tool_title(tool, parsed, cwd)
    for candidate in (preview, description):
        line = candidate.strip().splitlines()[0].strip() if candidate.strip() else ""
        if line:
            return line[:200]
    return tool


class SharedControl:
    """Implements A10 section 6.3 for sessions with a live channel attachment."""

    def __init__(self, hub: SessionHub) -> None:
        self.hub = hub

    # ------------------------------------------------------------- lifecycle

    async def registered(self, entry: SessionEntry, attachment: Attachment) -> None:
        previous = entry.shared
        if previous is not None and previous.attachment is not attachment:
            previous.attachment.detach()
        # Amendment A15: a terminal attached to it, so it is a live session again.
        await entry.channel.revive()
        entry.shared = SharedState(attachment=attachment)
        entry.holder_pid = attachment.pid or None
        entry.holder_identity = None
        entry.session.control = "shared"
        await entry.channel.emit("meta", control="shared")
        await self._settle(entry)
        await entry.channel.publish_summary()

    async def closed(self, entry: SessionEntry, attachment: Attachment, control: str) -> None:
        """Detach and hand the session back to the terminal, or to nobody."""
        state = entry.shared
        if state is None or state.attachment is not attachment:
            return
        entry.shared = None
        for approval in state.approvals.values():
            await self._emit_approval(entry, approval, status="expired")
        state.approvals.clear()
        state.by_channel.clear()
        self._drop_typed(state)
        await self._expire_question(entry, state)
        entry.session.control = control  # type: ignore[assignment]
        await entry.channel.emit("meta", control=control)
        if entry.session.turn is not None:
            await self._end_turn(entry, TurnEnd("stopped"))
        await entry.channel.set_state("readonly" if control == "terminal" else "idle")
        if entry.queue:
            # The held messages survive the detachment and go out through the
            # ordinary resume path the next time the session is sent to.
            await entry.channel.publish_queue(self.hub.queue_snapshot(entry))
        await entry.channel.publish_summary()

    async def forget(self, entry: SessionEntry) -> None:
        """Drop the attachment without publishing: the session itself is going.

        A hook waiting on a question would otherwise sit there until Claude Code
        times it out, a day later, holding a process for a session nobody can
        see any more.
        """
        state, entry.shared = entry.shared, None
        if state is None:
            return
        if state.question is not None:
            await state.question.hook.answer(None)
            state.question = None
        self._drop_typed(state)
        state.attachment.detach()

    # ------------------------------------------------------------ transcript

    async def tick(
        self,
        entry: SessionEntry,
        running: bool,
        trigger: str = "terminal",
        end: TurnEnd | None = None,
    ) -> None:
        """Called after every transcript read: the only source of turn state.

        `trigger` is what the rows just read say started the turn: the person at
        the keyboard, or another agent whose message the CLI filed as a user
        turn (amendment A30). An injection of our own names itself when it goes
        out and is still in flight here, which is what `waiting` protects.
        `end` is how those rows say the turn ended: `completed` unless the
        person interrupted it (amendment A32) or the vendor's usage limit
        stopped it (A35).
        """
        state = entry.shared
        if state is None:
            return
        if running and not state.running and not state.waiting:
            state.trigger = trigger
        state.running = running
        await self._settle(entry, end)
        if not running:
            await self.drain(entry)

    async def _settle(self, entry: SessionEntry, end: TurnEnd | None = None) -> None:
        """Reconcile turn, state and prompts with what the transcript says.

        An injection counts as busy before its row appears, so the apps see the
        turn start at once instead of at the next transcript read. Something
        waiting on the person outranks a running turn, and an approval outranks
        a question: it blocks the work the question was asked about. A turn
        that is over ends as `end` says, which only a transcript read has
        anything to say about.
        """
        state = entry.shared
        if state is None:
            return
        busy = state.busy
        if busy and entry.session.turn is None:
            await entry.channel.begin_turn(state.trigger)
        if state.approvals:
            await entry.channel.set_state("needs_approval")
        elif state.question is not None:
            await entry.channel.set_state("needs_input")
        elif busy:
            await entry.channel.set_state("running")
        else:
            await self._end_turn(entry, end or TurnEnd())

    async def _end_turn(self, entry: SessionEntry, end: TurnEnd) -> None:
        turn = entry.session.turn
        if turn is None:
            await entry.channel.set_state("idle")
            return
        started = int(turn.get("started_at") or now_ms())
        await entry.channel.end_turn(end.stop_reason, max(0, now_ms() - started), limit=end.limit)

    async def echo_delivered(self, entry: SessionEntry, message_id: str) -> None:
        """The injected row reached the transcript: the bubble is already correct."""
        state = entry.shared
        if state is not None and state.inflight == message_id:
            state.inflight = None

    async def echo_absorbed(self, entry: SessionEntry, message_id: str) -> None:
        """The CLI took the injection as mid-turn data; re-send it at the next idle."""
        state = entry.shared
        if state is None:
            return
        if state.inflight == message_id:
            state.inflight = None
        item = state.sent.pop(message_id, None)
        if item is None:
            return
        await self._emit_message(entry, item, "absorbed")
        if item.get("retried"):
            await entry.channel.notice(
                "warn", "the terminal treated this message as data; send it again if it was missed"
            )
            # The CLI read the message both times, and nothing more is queued,
            # so the bubble ends as delivered rather than on a promise of a
            # re-send that will not come (amendment A34).
            await self._emit_message(entry, item, "delivered")
            return
        item["retried"] = True
        # A43: back under its own `ts`, like every message the device puts
        # back, so an older one sent back meanwhile stays in front of it.
        hold(entry.queue, item, int(item["ts"]))
        await entry.channel.publish_queue(self.hub.queue_snapshot(entry))

    async def tool_finished(self, entry: SessionEntry, tool: str, failed: bool) -> None:
        """A tool ran or was refused, so its prompt was answered in the terminal."""
        state = entry.shared
        if state is None:
            return
        for request_id, approval in list(state.approvals.items()):
            if approval.tool != tool:
                continue
            self._forget(state, request_id)
            await self._resolve(entry, approval, DENY if failed else ALLOW, "terminal")
            return

    # ----------------------------------------------------------------- input

    async def send(
        self,
        entry: SessionEntry,
        request_id: str,
        text: str,
        attachments: list[dict[str, Any]],
        source: str = "remote",
        queue_ts: int | None = None,
    ) -> dict[str, Any]:
        state = entry.shared
        if state is None:
            raise RcError("conflict", "the session is no longer attached")
        if attachments:
            raise RcError("unsupported", "attachments cannot be delivered to a terminal session")
        # Amendment A15: held or injected, the message brings the session back.
        await entry.channel.revive()
        if source != "resume":
            # The resume prompt is the device's sentence, not a title (A35).
            await titles.from_prompt(entry.channel, text)
        item = pending_item(text, request_id, source)
        if state.injectable and await self._inject(entry, item):
            return {"accepted": "sent"}
        # Amendment A19: held is not delivered, and not a block either. The
        # bubble is drawn when the CLI takes the message, which is where the
        # terminal shows it too. An edited message is held where it was (A43).
        hold(entry.queue, item, queue_ts)
        await entry.channel.publish_queue(self.hub.queue_snapshot(entry))
        return {"accepted": "queued", "queued_id": str(item["id"])}

    async def drain(self, entry: SessionEntry) -> None:
        """Release the oldest held message once the transcript says the turn ended."""
        state = entry.shared
        if state is None or not state.injectable or not entry.queue:
            return
        state.inflight = None
        before = list(entry.queue)
        item = entry.queue.pop(0)
        if not await self._inject(entry, item):
            # The bridge refused it, so it goes back under its own `ts` (A43).
            # A message queued or removed while the bridge was being asked went
            # out in a snapshot without this one, which has to be put right.
            hold(entry.queue, item, int(item["ts"]))
            if entry.queue != before:
                await entry.channel.publish_queue(self.hub.queue_snapshot(entry))
            return
        await entry.channel.publish_queue(self.hub.queue_snapshot(entry))

    async def _inject(self, entry: SessionEntry, item: dict[str, Any]) -> bool:
        state = entry.shared
        if state is None:
            return False
        message_id = str(item["message_id"])
        if not await state.attachment.inject(message_id, str(item["text"])):
            return False
        state.mark_inflight(message_id)
        state.trigger = str(item.get("source") or "remote")
        state.remember(item)
        await self._emit_message(entry, item, "delivered")
        await self._settle(entry)
        return True

    async def _emit_message(self, entry: SessionEntry, item: dict[str, Any], delivery: str) -> None:
        await entry.channel.emit(
            "user_message",
            block_id=str(item["block_id"]),
            text=str(item["text"]),
            source=str(item.get("source") or "remote"),
            delivery=delivery,
        )

    # ---------------------------------------------------------------- typing

    def terminal(self, entry: SessionEntry) -> PtyLink:
        """The pseudo-terminal this session's CLI runs in, or the refusal (A40)."""
        state = entry.shared
        link = self.hub.ptys.get(state.attachment.pid) if state is not None else None
        if link is None:
            raise RcError(
                "conflict", "this terminal cannot be typed into; start it again to attach"
            )
        return link

    @staticmethod
    def _drop_typed(state: SharedState) -> None:
        """The terminal went away mid-script: whoever is waiting gets a refusal."""
        pending, state.typed = state.typed, None
        if pending is not None:
            pending.resolve(False)

    def expect(self, entry: SessionEntry, command: str, confirms: str = "") -> TypedCommand:
        """Claim the transcript rows the command about to be typed will leave."""
        state = entry.shared
        if state is None:
            raise RcError("conflict", "the session is no longer attached")
        pending = TypedCommand(
            command=command,
            confirms=confirms,
            done=asyncio.get_running_loop().create_future() if confirms else None,
        )
        state.typed = pending
        return pending

    def unclaim(self, entry: SessionEntry, pending: TypedCommand) -> None:
        """Give up the claim: the script failed, or nothing is coming."""
        state = entry.shared
        if state is not None and state.typed is pending:
            state.typed = None
        pending.resolve(False)

    async def set_settings(
        self, entry: SessionEntry, model: str | None, effort: str | None
    ) -> None:
        """Type the settings change into the terminal and wait for it to take.

        The lock is what keeps a person's message from being injected between
        two of the device's keystrokes; the reply to `session.set` waits for
        the transcript, so the `Session` an app receives is what runs now.
        """
        async with entry.lock:
            typist = Typist(entry, self.terminal(entry), self)
            if model is not None:
                await typist.set_model(model)
            if effort is not None:
                await typist.set_effort(effort)

    async def stop(self, entry: SessionEntry) -> None:
        """Type Escape into the terminal to stop the turn running there (A42).

        The lock is the same one the other scripts hold, so a Stop never lands
        between two keystrokes of a settings change. Nothing is waited for: the
        CLI files `[Request interrupted by user]` and the transcript reader
        ends the turn from that row (A32).
        """
        async with entry.lock:
            await Typist(entry, self.terminal(entry), self).interrupt()

    async def run_command(self, entry: SessionEntry, name: str, block_id: str) -> None:
        """Run one slash command by typing it (A27 through A40)."""
        if name != COMPACT_COMMAND.lstrip("/"):
            raise RcError("not_found", f"/{name} is not a command this session offers")
        async with entry.lock:
            await Typist(entry, self.terminal(entry), self).compact(block_id)

    async def typed_command(self, entry: SessionEntry, text: str) -> bool:
        """A `<command-name>` row: ours, or the person's own keystrokes (A32).

        True means the device typed it, so the row is not published: the app
        has its own bubble for a command it asked for, and a settings change
        is not a message at all.
        """
        state = entry.shared
        pending = state.typed if state is not None else None
        if pending is None or pending.expired:
            return False
        if text.split(" ", 1)[0] != pending.command:
            return False
        pending.seen = True
        if not pending.confirms and state is not None:
            state.typed = None
        return True

    async def command_output(self, entry: SessionEntry, text: str) -> None:
        """What the CLI printed back: the only thing that confirms a change."""
        state = entry.shared
        pending = state.typed if state is not None else None
        if pending is None or not pending.confirms or not pending.seen:
            return
        if state is not None:
            state.typed = None
        pending.resolve(pending.confirms in text.lower())

    # ------------------------------------------------------------- approvals

    async def permission_request(self, entry: SessionEntry, payload: dict[str, Any]) -> None:
        state = entry.shared
        if state is None:
            return
        channel_id = str(payload.get("request_id") or "")
        if not channel_id or channel_id in state.by_channel:
            return
        tool = str(payload.get("tool_name") or "Tool")
        description = str(payload.get("description") or "")
        preview = str(payload.get("input_preview") or "")
        approval = SharedApproval(
            request_id=str(uuid.uuid4()),
            channel_id=channel_id,
            block_id=str(uuid.uuid4()),
            tool=tool,
            title=approval_title(tool, preview, description, entry.session.cwd),
            input={"tool_name": tool, "description": description, "input_preview": preview},
        )
        state.approvals[approval.request_id] = approval
        state.by_channel[channel_id] = approval.request_id
        await self._emit_approval(entry, approval, status="pending")
        await self._settle(entry)

    async def approve(self, entry: SessionEntry, request_id: str, option_id: str) -> dict[str, Any]:
        state = entry.shared
        if state is None:
            raise RcError("conflict", "the session is no longer attached")
        if option_id not in BEHAVIORS:
            raise RcError("bad_request", "a shared session accepts only allow or deny")
        approval = state.approvals.get(request_id)
        if approval is None:
            # Answered in the terminal, or from another app first. Claude Code
            # drops unknown ids silently, so this is a no-op either way.
            return {}
        self._forget(state, request_id)
        await state.attachment.relay_permission(approval.channel_id, option_id)
        await self._resolve(entry, approval, option_id, "remote")
        return {}

    @staticmethod
    def _forget(state: SharedState, request_id: str) -> None:
        approval = state.approvals.pop(request_id, None)
        if approval is not None:
            state.by_channel.pop(approval.channel_id, None)

    async def _resolve(
        self, entry: SessionEntry, approval: SharedApproval, option_id: str, by: str
    ) -> None:
        await self._emit_approval(
            entry, approval, status="resolved", decision={"option_id": option_id, "by": by}
        )
        await self._settle(entry)

    # ------------------------------------------------------------- questions

    async def question(self, entry: SessionEntry, hook: HookQuestion) -> None:
        """Raise the CLI's own question as a block an app can answer (A20)."""
        state = entry.shared
        if state is None:
            await hook.answer(None)
            return
        try:
            questions = normalise_questions(hook.input)
        except ValueError as exc:
            log.warning("unusable question from the hook", reason=str(exc))
            await hook.answer(None)
            return
        # A second question while one is open means the first will never be
        # answered: the CLI has moved on, whatever its hook is still waiting for.
        await self._expire_question(entry, state)
        request_id = str(uuid.uuid4())
        state.question = SharedQuestion(
            request_id=request_id,
            block_id=f"question:{request_id}",
            questions=questions,
            hook=hook,
        )
        await self._emit_question(entry, state.question, status="pending")
        await self._settle(entry)

    async def answer(
        self, entry: SessionEntry, request_id: str, answers: dict[str, Any]
    ) -> dict[str, Any]:
        state = entry.shared
        if state is None:
            raise RcError("conflict", "the session is no longer attached")
        question = state.question
        if question is None or question.request_id != request_id:
            # Answered in the terminal, or from another app first; either way
            # the CLI has what it needs and this reply changes nothing.
            return {}
        state.question = None
        await question.hook.answer(answers_for_tool(question.questions, answers))
        await self._emit_question(entry, question, status="resolved", answers=answers, by="remote")
        await self._settle(entry)
        return {}

    async def question_answered(self, entry: SessionEntry, result: Any) -> None:
        """The transcript shows the tool ran: the terminal answered first."""
        state = entry.shared
        if state is None or state.question is None:
            return
        question, state.question = state.question, None
        await question.hook.answer(None)
        answers = answers_from_tool(question.questions, result)
        fields: dict[str, Any] = {"answers": answers} if answers else {}
        await self._emit_question(entry, question, status="resolved", by="terminal", **fields)
        await self._settle(entry)

    async def question_closed(self, entry: SessionEntry, hook: HookQuestion) -> None:
        """Claude Code stopped the hook, so nobody can answer this block now."""
        state = entry.shared
        if state is None or state.question is None or state.question.hook is not hook:
            return
        await self._expire_question(entry, state)
        await self._settle(entry)

    async def _expire_question(self, entry: SessionEntry, state: SharedState) -> None:
        question, state.question = state.question, None
        if question is None:
            return
        await question.hook.answer(None)
        await self._emit_question(entry, question, status="expired")

    @staticmethod
    async def _emit_question(
        entry: SessionEntry, question: SharedQuestion, *, status: str, **fields: Any
    ) -> None:
        await entry.channel.emit(
            "question",
            block_id=question.block_id,
            request_id=question.request_id,
            questions=[dict(item) for item in question.questions],
            status=status,
            **fields,
        )

    async def _emit_approval(
        self,
        entry: SessionEntry,
        approval: SharedApproval,
        *,
        status: str,
        decision: dict[str, str] | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "block_id": approval.block_id,
            "request_id": approval.request_id,
            "tool": approval.tool,
            "tool_kind": tool_kind(approval.tool),
            "title": approval.title,
            "input": dict(approval.input),
            "options": [dict(option) for option in APPROVAL_OPTIONS],
            "status": status,
        }
        if decision is not None:
            fields["decision"] = decision
        await entry.channel.emit("approval", **fields)
