"""Amendments A10 and A51: driving a terminal session the device is attached to.

The one rule the whole file exists for: a message the channel delivers while a
turn is running is reframed by the CLI as untrusted external data the model is
told not to obey, so the device never injects into a running turn. What it does
instead depends on how the message was sent (A51):

- `auto` steers. The shim's settings make the CLI run the device's turn hooks
  (`channel/turnhook.py`), and at the turn's next batch of tool calls the
  message is handed to the model as that batch's additional context — where
  Claude Code itself hands over a message the person types mid-turn — with a
  line in the terminal saying so. A turn with no batch left takes it at its
  `Stop` hook instead: written into the channel before the CLI goes idle, it
  starts the next turn, beside whatever the person queued, the way the CLI
  batches the person's own queued messages.
- `queue` waits as a queue entry and nothing else until it is injected (A19,
  A43), one per turn's end, at the same `Stop` hook.

A CLI that runs no hooks of ours — started with somebody's own `--settings` —
holds both as A19 says, and they go in when the transcript shows the turn
ended. Turn state comes from the hooks first and the transcript second: the
transcript lags reality by up to one tail interval, and only it shows a turn
another agent's hidden message starts. An injection marks itself in flight
until its own row appears. What decides injecting is the CLI's own turn:
subagents still at work are shown, because working means all of it, but never
hold a message back, since the CLI takes one between them as readily as the
person's.

A message the CLI takes as data anyway is put back and injected again at the
next turn's end, at most three times; absorbed once more, it is an `error` the
apps show, and its bubble never claims a delivery that did not happen. Only
evidence makes a message lost — the CLI removing it, or going past it without
its row — never the time gone by, which only stops the device waiting on it.

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
Attachments ride in the text as well (A49): a message's files are written when
it is sent, and the words the CLI receives end with their paths.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .. import attachments
from ..agents.claude.questions import answers_for_tool, answers_from_tool
from ..agents.claude.tools import reads_session_file, tool_kind, tool_title
from ..agents.questions import normalise_questions
from ..errors import RcError
from ..logging_setup import logger
from ..models import now_ms
from . import titles
from .attach import Attachment, HookQuestion, TurnHook
from .limits import TurnEnd
from .ptys import PtyLink
from .queue import hold, release
from .typist import COMPACT_COMMAND, TypedCommand, Typist

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from .hub import SessionEntry, SessionHub

log = logger("rc_client.shared")

# How long an injection may stay unconfirmed before the next one is allowed.
INFLIGHT_TIMEOUT = 30.0
# How long to wait after a bridge disconnects before deciding whether the CLI
# process is still there: `/exit` closes the channel about a second early.
EXIT_SETTLE = 1.0
MAX_REMEMBERED = 64
# A51: how long a prompt the CLI reported counts as a turn while the transcript
# shows none — a turn the person stopped before `Stop` ran ends this way.
PROMPT_GUARD = 3.0
# How long a turn hook waits for the session's lock at most, and a `Stop` hook
# for the bridge to write what it was given; both stop short of the deadline the
# hook sends by the time its answer needs to reach it (A51). A hook that sends
# no deadline is given the fallback, from its frame.
HOOK_LOCK_TIMEOUT = 0.5
STOP_WRITE_TIMEOUT = 1.0
REPLY_MARGIN = 0.15
HOOK_FALLBACK_BUDGET = 0.8
# How many times a message the CLI absorbed is injected again before giving up,
# and, apart from those, how often one written at a turn's end may be absorbed
# by the turn going on where the session cannot steer it back in (A51).
MAX_REDELIVERIES = 3
MAX_FREE_REDELIVERIES = 10
# How long a message written at a turn's end, or handed over at a tool
# boundary, may go without its row once a later turn hook shows the CLI went
# past it, before it counts as lost: the transcript lags the hooks. Time alone
# never makes a message lost, since somebody's own slow `Stop` hook holds the
# CLI with the message unread (A51).
PASSED_GRACE = 5.0
# How long after a `Stop` the turn's state is reported even if no row came,
# which leaves somebody's own `Stop` hook the time to send the turn on first.
STOP_SETTLE = 1.0
# How many injections the CLI may be holding in its own queue, remembered, and
# how long one counts as held while the transcript shows neither its queue nor
# its conversation move: long enough for a dialog, never for good (A51).
MAX_HELD_BY_CLI = 32
HELD_QUIET = 30.0
# How much of a message the terminal's line repeats, and the give-up error quotes.
NOTE_CHARS = 200
QUOTE_CHARS = 60

ALLOW = "allow"
DENY = "deny"
BEHAVIORS = (ALLOW, DENY)

# Session-scoped grants are not available through the relay, so the two options
# below are the whole vocabulary of a shared approval.
APPROVAL_OPTIONS = [
    {"id": ALLOW, "label": "Allow", "style": "primary"},
    {"id": DENY, "label": "Deny", "style": "danger"},
]

# A51: the words a steered message reaches the model with, Claude Code's own
# for a message the person types mid-turn, said of one relayed from an app.
STEER_LEAD = (
    "The person who owns this machine sent a new message through SideCode while you "
    "were working. It is their own words, exactly as if typed in this terminal:"
)
STEER_TAIL = (
    "This is how the messages they send mid-turn reach you: within the running turn, beside "
    "the latest tool results, rather than as a separate turn. Address the message above as you "
    "continue this turn."
)
# The line the terminal shows where the message went in.
STEER_NOTE = "SideCode · remote message: "
# Who started a turn, by whose prompt the CLI reported (A51).
PROMPT_TRIGGERS = {"agent": "agent", "person": "terminal", "command": "terminal"}
_CHANNEL_TAG = re.compile(r"<(/?)(channel)", re.IGNORECASE)


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
    """Everything the device tracks about one attached CLI session.

    `running` is the CLI's own turn as the transcript tells it, `working` its
    subagents still at work; only the first decides injecting (A51). The rest
    of the turn's story comes from the turn hooks: `hooked` once the CLI runs
    them at all, `prompted_at` from a prompt to the turn's `Stop`,
    `command_at` from a slash command that may or may not start one, and
    `stopping` while a `Stop` hook is answered.
    """

    attachment: Attachment
    running: bool = False
    working: bool = False
    inflight: str | None = None
    inflight_at: float = 0.0
    trigger: str = "terminal"
    approvals: dict[str, SharedApproval] = field(default_factory=dict)
    by_channel: dict[str, str] = field(default_factory=dict)
    # One question at a time: the CLI asks the person one thing and waits.
    question: SharedQuestion | None = None
    # The command the device is typing into the terminal right now (A40).
    typed: TypedCommand | None = None
    hooked: bool = False
    prompted_at: float | None = None
    command_at: float | None = None
    stopping: bool = False
    # Messages steered into the running turn and not handed over yet, oldest
    # first, and the ones handed over at a tool boundary until their row shows.
    steers: list[dict[str, Any]] = field(default_factory=list)
    handed: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Injections the CLI has queued and not started, which a dialog may hold,
    # and when the transcript last showed the CLI's queue or conversation move.
    held_by_cli: dict[str, None] = field(default_factory=dict)
    queue_seen_at: float = 0.0
    # The injection in flight was written at a turn's end, to be the next turn,
    # and the turn the apps were shown then, which its own row ends at the latest.
    inflight_at_stop: bool = False
    turn_at_stop: dict[str, Any] | None = None

    def mark_inflight(self, message_id: str, at_stop: bool = False) -> None:
        self.inflight = message_id
        self.inflight_at = time.monotonic()
        self.inflight_at_stop = at_stop

    @property
    def waiting(self) -> bool:
        """True while an injection has not yet shown up in the transcript.

        One the CLI says it has queued waits as long as the CLI does, unless
        nothing in the transcript has moved for `HELD_QUIET`: a session must not
        stay busy for good. Either timeout only stops the wait; it never makes
        the message lost (A51).
        """
        if self.inflight is None:
            return False
        if self.inflight in self.held_by_cli:
            return (time.monotonic() - self.queue_seen_at) < HELD_QUIET
        return (time.monotonic() - self.inflight_at) < INFLIGHT_TIMEOUT

    @property
    def busy(self) -> bool:
        """Whether the CLI's own turn is under way, which is what injecting waits for.

        An injection counts before its row reaches the transcript: the CLI has
        the message, so the turn has started even though the tailer has not
        caught up. A prompt a turn hook reported counts the same way (A51).
        What runs here is what Stop presses Escape on.
        """
        return (
            self.running
            or self.waiting
            or self.prompted_at is not None
            or self.command_at is not None
            or self.stopping
        )

    @property
    def shown_busy(self) -> bool:
        """Whether the apps are told the session runs: its subagents count here.

        A slash command is not shown until a turn follows it, and a message
        written at a turn's end not until its own turn starts, so the turn it
        follows can end first (A51).
        """
        own = self.running or self.prompted_at is not None or self.stopping
        return own or (self.waiting and not self.inflight_at_stop) or self.working

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

    def lapse_prompt(self) -> None:
        """A prompt no `Stop` closed and no turn followed is not a turn any more."""
        now = time.monotonic()
        if self.prompted_at is not None and now - self.prompted_at >= PROMPT_GUARD:
            self.prompted_at = None
        if self.command_at is not None and now - self.command_at >= PROMPT_GUARD:
            self.command_at = None


def remember(entry: SessionEntry, item: dict[str, Any]) -> None:
    """Keep an injection by its id, so its row or its absorption finds it again.

    The record lives on the session rather than the attachment, so a bridge
    that registers again still knows what it was sent (A51).
    """
    entry.sent[str(item["message_id"])] = item
    for key in list(entry.sent)[:-MAX_REMEMBERED]:
        entry.sent.pop(key, None)


def pending_item(
    text: str,
    request_id: str,
    source: str = "remote",
    uploads: list[dict[str, Any]] | None = None,
    written: list[attachments.Attachment] | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """One held message: a queue entry that also carries its bubble's identity.

    The bubble is the app's request id (amendment A12), which is also the id
    the queue reports, so a message drawn on sending and shown again as queued
    and once more as delivered is one block throughout. `message_id` stays the
    channel's own: it names the injection, not the bubble. `source` is what the
    bubble says the message was: a person's, or the device's resume (A35).
    `attachments` keeps the bytes the app sent until the CLI has the message,
    and `written` the files they became, which its words name (A49). `arrived`
    orders it against the person's prompts, `retries` counts its injections
    after the first that the CLI absorbed, and `mode` is how the app sent it,
    which decides how it goes in again (A51).
    """
    message_id = str(uuid.uuid4())
    block_id = request_id or str(uuid.uuid4())
    return {
        "id": block_id,
        "text": text,
        "ts": now_ms(),
        "attachments": list(uploads or []),
        "written": list(written or []),
        "message_id": message_id,
        "block_id": block_id,
        "arrived": time.monotonic(),
        "retries": 0,
        "source": source,
        "mode": mode,
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


def previewed_input(preview: str) -> dict[str, Any]:
    """The tool input the relay's preview carries as JSON, or nothing."""
    try:
        parsed = json.loads(preview)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sent_order(item: dict[str, Any]) -> tuple[int, float]:
    """Where a message stands in send order: its `ts`, which an edit keeps (A43),
    and within one millisecond, when the device took it."""
    return int(item["ts"]), float(item.get("arrived") or 0.0)


def _gone_past(item: dict[str, Any]) -> bool:
    """Whether a turn hook showed the CLI past this message long enough ago that
    its row, had it come, would have been read by now (A51)."""
    passed = item.get("passed_at")
    return passed is not None and time.monotonic() - float(passed) >= PASSED_GRACE


def _one_line(text: str, limit: int) -> str:
    said = " ".join(text.split())
    return said if len(said) <= limit else said[: limit - 1] + "…"


def _defused(prompt: str) -> str:
    """The words with any channel tag in them made plain text."""
    return _CHANNEL_TAG.sub(r"&lt;\1\2", prompt)


def steer_context(prompts: list[tuple[str, str]]) -> str:
    """What a tool boundary hands the model: each message in the channel's own tag.

    The tag is the one the CLI wraps a channel message in, which the channel's
    instructions call the owner's words, and its id is what the transcript
    gives back. A tag inside the words is defused, so it cannot pose as one.
    """
    tags = "\n".join(
        f'<channel source="rc" origin="app" message_id="{message_id}">\n'
        f"{_defused(prompt)}\n</channel>"
        for message_id, prompt in prompts
    )
    return f"{STEER_LEAD}\n{tags}\n\n{STEER_TAIL}"


def steer_note(items: list[dict[str, Any]]) -> str:
    """The terminal's line for what went in: the words, never the files' paths."""
    lines = []
    for item in items:
        said = _one_line(str(item["text"]), NOTE_CHARS)
        files = len(item.get("written") or [])
        if not said:
            said = f"{files} file" if files == 1 else f"{files} files"
        lines.append(STEER_NOTE + said)
    return "\n".join(lines)


class TranscriptReader(Protocol):
    """What reads the transcripts of attached sessions: the mirror, in the daemon."""

    def read_soon(self) -> None: ...

    def settle_soon(self, session_id: str, delay: float) -> None: ...


@dataclass(slots=True)
class SharedSteering:
    """An attached session as the hub steers it (A51): `AttachmentSteering`'s shape.

    It steers only a CLI that runs the turn hooks, `PostToolBatch` among them
    (`runs_tool_batch`), and only while its turn is under way; anything else
    says no, and the hub sends the message as usual.
    """

    control: SharedControl
    entry: SessionEntry
    source: str

    @property
    def supports_steer(self) -> bool:
        state = self.entry.shared
        return state is not None and state.hooked and self.control.runs_tool_batch(state)

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        return await self.steer_with_attachments(text, [], block_id)

    async def steer_with_attachments(
        self, text: str, attachments: list[dict[str, Any]], block_id: str | None = None
    ) -> bool:
        return await self.control.steer(self.entry, block_id or "", text, attachments, self.source)


class SharedControl:
    """Implements A10 section 6.3 for sessions with a live channel attachment."""

    def __init__(self, hub: SessionHub) -> None:
        self.hub = hub
        # Set by the mirror while it runs. Turn state and every confirmation
        # come from what it reads; on its own the hub has no reader at all.
        self.reader: TranscriptReader | None = None

    # ------------------------------------------------------------- lifecycle

    async def registered(self, entry: SessionEntry, attachment: Attachment) -> None:
        previous = entry.shared
        if previous is not None and previous.attachment is not attachment:
            previous.attachment.detach()
        # Amendment A15: a terminal attached to it, so it is a live session again.
        await entry.channel.revive()
        state = SharedState(attachment=attachment)
        if previous is not None:
            # A51: the same session registering again keeps what it owes the CLI,
            # and what the CLI said it holds: its process, and its queue, are the same.
            state.steers, state.handed = previous.steers, previous.handed
            state.held_by_cli, state.queue_seen_at = previous.held_by_cli, previous.queue_seen_at
        entry.shared = state
        entry.holder_pid = attachment.pid or None
        entry.holder_identity = None
        entry.session.control = "shared"
        await entry.channel.emit("meta", control="shared")
        await self._settle(entry)
        await entry.channel.publish_summary()
        # Its transcript is looked for now, not at the next scan: turn state,
        # and every change typed into the terminal, are read from it.
        self.read_soon()

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
        # A51: a message handed to the model, or written for the next turn, is
        # drawn even if its row is never read now; a steer the CLI never got
        # waits with the held messages.
        unshown = [item for item in entry.sent.values() if item.pop("at_stop", False)]
        for item in [*state.handed.values(), *unshown]:
            await self._emit_message(entry, item, "delivered")
        state.handed.clear()
        for item in state.steers:
            hold(entry.queue, item, int(item["ts"]))
        state.steers.clear()
        if entry.queue:
            # The held messages survive the detachment and go out through the
            # ordinary resume path the next time the session is sent to. Their
            # files do not: whatever delivers them writes them again (A49).
            for item in entry.queue:
                await release(item)
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
        for item in state.steers:
            await release(item)
        state.attachment.detach()

    # ------------------------------------------------------------ transcript

    async def tick(
        self,
        entry: SessionEntry,
        running: bool,
        trigger: str = "terminal",
        end: TurnEnd | None = None,
        working: bool = False,
    ) -> None:
        """Called after every transcript read: turn state as the rows tell it.

        `running` is the CLI's own turn and `working` its subagents too (A51).
        `trigger` is what the rows just read say started the turn: the person
        at the keyboard, or another agent whose message the CLI filed as a user
        turn (amendment A30). An injection of our own names itself when it goes
        out and is still in flight here, which is what `waiting` protects.
        `end` is how those rows say the turn ended: `completed` unless the
        person interrupted it (amendment A32) or the vendor's usage limit
        stopped it (A35). A held message is handed over by the hub's drain
        task, never from here: a settings change being typed holds the
        session's lock until this reader confirms it (A40).
        """
        state = entry.shared
        if state is None:
            return
        state.working = working
        if running and not state.running and not state.waiting:
            state.trigger = trigger
        state.running = running
        if not running:
            state.lapse_prompt()
        await self._recover_handed(entry, state)
        await self._recover_at_stop(entry, state)
        await self._settle(entry, end)
        if state.injectable and (entry.queue or state.steers):
            self.hub.schedule_drain(entry)

    async def _settle(self, entry: SessionEntry, end: TurnEnd | None = None) -> None:
        """Reconcile turn, state and prompts with what the transcript says.

        An injection counts as busy before its row appears, so the apps see the
        turn start at once instead of at the next transcript read; subagents
        still at work keep the turn open for the apps, though not for the
        CLI (A51). Something waiting on the person outranks a running turn,
        and an approval outranks a question: it blocks the work the question
        was asked about. A turn that is over ends as `end` says, which only a
        transcript read has anything to say about.
        """
        state = entry.shared
        if state is None:
            return
        busy = state.shown_busy
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
        """The injected row reached the transcript: the CLI has the message.

        A message written at a turn's end is drawn only now, after the turn it
        followed, which ends here for the apps if no read had ended it yet; it
        starts a turn of its own (A51). So is one drawn as absorbed before it
        went in again; a second row for a message the CLI took draws nothing.
        The CLI has the words now, and the files they name are on disk, so the
        bytes the app sent are not needed again; only their count is kept (A49).
        """
        state = entry.shared
        if state is not None:
            if state.inflight == message_id:
                state.inflight = None
                state.inflight_at_stop = False
            state.held_by_cli.pop(message_id, None)
        await self._took_after_all(entry, message_id)
        item = entry.sent.get(message_id)
        if item is None:
            return
        item.pop("at_stop", None)
        item["taken"] = True
        if item.get("shown") != "delivered" and state is not None:
            ended, state.turn_at_stop = state.turn_at_stop, None
            if ended is not None and entry.session.turn is ended:
                await self._end_turn(entry, TurnEnd())
            await self._emit_message(entry, item, "delivered")
            state.trigger = str(item.get("source") or "remote")
            state.running = True
            await self._settle(entry)
        item["attachments"] = attachments.wire_attachments(item.get("written") or [])

    async def _took_after_all(self, entry: SessionEntry, message_id: str) -> None:
        """A message given up for lost reached the CLI after all: its copy goes (A51).

        A copy still waiting to go in again is taken out of the queue or the
        steers. One already handed to the model at a tool boundary cannot be
        taken back, nor one written into the channel, and the model reads the
        words twice; either way the bubble says delivered once, the written
        copy's own row drawing it, and nothing counts or goes in again.
        """
        written = entry.sent.get(message_id)
        if written is not None and written.pop("lost", False):
            written.pop("at_stop", None)
            written["taken"] = True
            return
        state = entry.shared
        handed = state.handed.get(message_id) if state is not None else None
        if state is not None and handed is not None and handed.get("lost"):
            del state.handed[message_id]
            remember(entry, handed)
            await self._taken(entry, handed)
            return
        holders = [entry.queue, *([state.steers] if state is not None else [])]
        for holder in holders:
            copy = next(
                (item for item in holder if item.get("lost") and item["message_id"] == message_id),
                None,
            )
            if copy is None:
                continue
            holder.remove(copy)
            remember(entry, copy)
            await self._taken(entry, copy)
            if holder is entry.queue:
                await entry.channel.publish_queue(self.hub.queue_snapshot(entry))
            return

    async def _taken(self, entry: SessionEntry, item: dict[str, Any]) -> None:
        """The CLI took this message as one: its bubble says so once, and it never goes in again."""
        item.pop("lost", None)
        item["taken"] = True
        if item.get("shown") != "delivered":
            await self._emit_message(entry, item, "delivered")

    async def echo_queued(self, entry: SessionEntry, message_id: str) -> None:
        """The CLI queued an injection it has not started: it waits on the CLI (A51)."""
        state = entry.shared
        if state is None or message_id not in entry.sent:
            return
        state.held_by_cli[message_id] = None
        state.queue_seen_at = time.monotonic()
        for stale in list(state.held_by_cli)[:-MAX_HELD_BY_CLI]:
            state.held_by_cli.pop(stale, None)

    async def queue_moved(self, entry: SessionEntry) -> None:
        """The transcript shows the CLI's queue or conversation moving (A51)."""
        state = entry.shared
        if state is not None:
            state.queue_seen_at = time.monotonic()

    async def echo_absorbed(self, entry: SessionEntry, message_id: str) -> None:
        """The CLI took the injection as mid-turn data; it goes in again (A51).

        One written at a turn's end and absorbed all the same was overtaken by
        a turn that went on, by somebody's own `Stop` hook for one, which is
        nothing the message did: that does not count against its re-sends. A
        message the CLI already took as one, a copy's twin, is only forgotten.
        """
        item = self._let_go(entry, message_id)
        if item is not None and not item.get("taken"):
            await self._put_back(entry, item, counted=not item.get("at_stop"))

    async def echo_dropped(self, entry: SessionEntry, message_id: str) -> None:
        """The CLI removed a queued injection without running it; it goes in again (A51).

        Losing a message is worse than sending it twice, so it is re-sent, and
        that counts against its re-sends; if it reaches the CLI after all, as a
        prompt or a row of its own, the copy is withdrawn (`_took_after_all`).
        """
        item = self._let_go(entry, message_id)
        if item is not None and not item.get("taken"):
            await self._lost(entry, item)

    async def echo_pulled(self, entry: SessionEntry, message_id: str) -> None:
        """The person pulled a queued injection back into the input box to edit (A51).

        It is theirs to send now: the CLI no longer holds it, nothing is sent
        again and its bubble stays as it is; if it reaches the model after
        all, its row or its prompt says delivered as usual. One written at a
        turn's end that the person then deletes is never drawn, and no error
        says so: what they do with words they took back is theirs.
        """
        state = entry.shared
        if state is None:
            return
        if state.inflight == message_id:
            state.inflight = None
            state.inflight_at_stop = False
        state.held_by_cli.pop(message_id, None)
        item = entry.sent.get(message_id)
        if item is not None:
            item.pop("at_stop", None)

    async def _lost(self, entry: SessionEntry, item: dict[str, Any]) -> None:
        """A message the CLI let go goes in again, counted, ready to be withdrawn."""
        item.pop("at_stop", None)
        item["lost"] = True
        await self._put_back(entry, item, counted=True)

    async def _recover_at_stop(self, entry: SessionEntry, state: SharedState) -> None:
        """A message written at a turn's end that the CLI went past goes in again (A51).

        Only evidence makes it lost, never the time gone by: a turn hook that
        ran after it was written while neither its row nor the CLI's queue
        showed it (`_passed`), and `PASSED_GRACE` for the transcript to catch
        up. It is then sent again, counted, as a removed one is. One the CLI
        says it holds is never judged here: only its removal or its absorption
        says what became of it.
        """
        for message_id, item in list(entry.sent.items()):
            if not item.get("at_stop") or message_id in state.held_by_cli or not _gone_past(item):
                continue
            lost = self._let_go(entry, message_id)
            if lost is not None and not lost.get("taken"):
                log.warning("a message written at a turn's end never showed in the transcript")
                await self._lost(entry, lost)

    @staticmethod
    def _let_go(entry: SessionEntry, message_id: str) -> dict[str, Any] | None:
        """Forget an injection as in flight or held, and hand back what it was."""
        state = entry.shared
        if state is not None:
            if state.inflight == message_id:
                state.inflight = None
                state.inflight_at_stop = False
            state.held_by_cli.pop(message_id, None)
        return entry.sent.pop(message_id, None)

    async def _put_back(self, entry: SessionEntry, item: dict[str, Any], counted: bool) -> None:
        """Put a message the CLI did not take as one back in line, or give it up.

        A bubble already drawn becomes `absorbed` ("will be re-sent"). When the
        session steers, a message written at a turn's end and absorbed because
        that turn went on — somebody's own `Stop` hook sent it on, so it had not
        ended — goes back into it at its next batch of tool calls whatever its
        mode, uncounted; so does any other sent with `mode: "auto"`, counted.
        The resume prompt of A35 never steers. The rest go back into the queue
        under their own `ts`, so an older one sent back meanwhile stays in
        front (A43), and in again at the next turn's end. Counted re-sends stop
        at three, uncounted ones at ten; past either, a message is an error
        under the bubble's own block id, which the apps put in the bubble's
        place: nothing delivered it (A51).
        """
        at_stop = bool(item.pop("at_stop", False))
        item.pop("passed_at", None)
        if item.get("shown"):
            await self._emit_message(entry, item, "absorbed")
        state = entry.shared
        steerable = (
            state is not None
            and state.hooked
            and self.runs_tool_batch(state)
            and item.get("source") != "resume"
        )
        free = at_stop and not counted
        if not (free and steerable):
            key, limit = (
                ("free_retries", MAX_FREE_REDELIVERIES) if free else ("retries", MAX_REDELIVERIES)
            )
            tries = int(item.get(key) or 0)
            if tries >= limit:
                await self._give_up(entry, item, tries + 1)
                return
            item[key] = tries + 1
        if state is not None and steerable and (free or item.get("mode") == "auto"):
            state.steers.append(item)
            return
        hold(entry.queue, item, int(item["ts"]))
        await entry.channel.publish_queue(self.hub.queue_snapshot(entry))

    @staticmethod
    async def _give_up(entry: SessionEntry, item: dict[str, Any], tries: int) -> None:
        """No more re-sends: an error in the bubble's place, under its block id (A51)."""
        await release(item)
        quote = _one_line(str(item["text"]), QUOTE_CHARS)
        await entry.channel.error(
            f'The terminal did not take "{quote}" as a message after {tries} tries; send it again.',
            code="conflict",
            block_id=str(item["block_id"]),
        )

    async def steer_delivered(self, entry: SessionEntry, message_ids: list[str]) -> None:
        """The transcript shows the model got these steered messages: their bubbles (A51).

        They are drawn here, not when the hook handed them over, so they take
        their place beside whatever the CLI folded into the turn at the same
        boundary in the order the model reads them. One given up for lost and
        sent again meanwhile reached the model after all: its copy goes as
        `_took_after_all` says, and the bubble is drawn now.
        """
        state = entry.shared
        if state is None:
            return
        for message_id in message_ids:
            item = state.handed.pop(message_id, None)
            if item is None:
                await self._took_after_all(entry, message_id)
                item = entry.sent.get(message_id)
                if item is None or not item.get("taken"):
                    continue
            await self._taken(entry, item)
            item["attachments"] = attachments.wire_attachments(item.get("written") or [])

    async def _recover_handed(self, entry: SessionEntry, state: SharedState) -> None:
        """A message handed over at a tool boundary whose row never came goes in again.

        Only once a turn hook showed the CLI past that boundary and the
        transcript has had `PASSED_GRACE` to show the row (`_passed`); it then
        counts, as a removed message does, and its copy is withdrawn should
        the row come after all (A51).
        """
        for message_id, item in list(state.handed.items()):
            if not _gone_past(item):
                continue
            state.handed.pop(message_id, None)
            log.warning("a message handed to the model never showed in the transcript")
            await self._lost(entry, item)

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

    # ----------------------------------------------------------- turn hooks

    async def turn_hook(self, entry: SessionEntry, hook: TurnHook) -> dict[str, str]:
        """One of the CLI's turn hooks (A51): what it reports, and what goes back.

        A prompt is answered at once. A batch of tool calls and a turn's end
        with something to deliver take the session's lock, as a send does, so a
        message is never handed over twice. The hook sends its deadline: what
        cannot be done while the answer can still reach it is not begun, and
        the next boundary, or the transcript's turn end, delivers instead. A
        CLI started without the batch hook delivers nothing at `Stop` either:
        the transcript's turn end does, as A19 has it.
        """
        state = entry.shared
        if state is None:
            return {}
        state.hooked = True
        self._passed(entry, state, hook)
        if hook.event == "prompt":
            await self._prompted(entry, state, hook)
            return {}
        deadline = hook.deadline or time.time() + HOOK_FALLBACK_BUDGET
        if hook.event == "stop":
            self._turn_over(state)
            if self.reader is not None:
                self.reader.settle_soon(entry.session.session_id, STOP_SETTLE)
            if not self.runs_tool_batch(state) or not self._owes_turn_end(entry, state):
                return {}
        wait = min(HOOK_LOCK_TIMEOUT, deadline - REPLY_MARGIN - time.time())
        if wait <= 0:
            return {}
        try:
            await asyncio.wait_for(entry.lock.acquire(), wait)
        except TimeoutError:
            return {}
        try:
            if entry.shared is not state:
                return {}
            if hook.event == "batch":
                return await self._tool_boundary(entry, state, deadline)
            await self._turn_end(entry, state, deadline)
            return {}
        finally:
            entry.lock.release()

    def _passed(self, entry: SessionEntry, state: SharedState, hook: TurnHook) -> None:
        """A turn hook ran: the CLI is past what it was written or handed before (A51).

        That is the evidence a message whose row has not shown may be lost
        (`_recover_at_stop`, `_recover_handed`); the time gone by is not, since
        somebody's own slow `Stop` hook holds the CLI with the message unread
        and runs no other hook meanwhile. The prompt that is the message itself
        is its arrival, not a step past it, and one the CLI says it holds waits
        on the CLI. The transcript is read now, and the turn's state reported
        again once it has had `PASSED_GRACE` to show what it lagged behind.
        """
        named = hook.message_id if hook.event == "prompt" else ""
        written = [
            item
            for message_id, item in entry.sent.items()
            if item.get("at_stop") and message_id not in state.held_by_cli
        ]
        now = time.monotonic()
        passed = False
        for item in [*written, *state.handed.values()]:
            if item["message_id"] != named and "passed_at" not in item:
                item["passed_at"] = now
                passed = True
        if passed and self.reader is not None:
            self.reader.read_soon()
            self.reader.settle_soon(entry.session.session_id, PASSED_GRACE)

    async def _prompted(self, entry: SessionEntry, state: SharedState, hook: TurnHook) -> None:
        """A prompt went into the CLI: a turn starts, or the person queued one behind it.

        Our own injection taken as a prompt is no longer held by the CLI. A
        slash command may start a turn — a skill, a command of the person's —
        or only open a dialog, so it holds injecting back for a moment and
        shows nothing until the transcript says which.
        """
        now = time.monotonic()
        if hook.prompt_kind == "ours" and hook.message_id:
            state.held_by_cli.pop(hook.message_id, None)
            state.queue_seen_at = now
            await self._took_after_all(entry, hook.message_id)
        if not state.busy:
            state.trigger = PROMPT_TRIGGERS.get(hook.prompt_kind, state.trigger)
        if hook.prompt_kind == "command":
            state.command_at = now
            return
        state.prompted_at = now
        await self._settle(entry)

    def runs_tool_batch(self, state: SharedState) -> bool:
        """Whether this CLI was started with the `PostToolBatch` hook (A51).

        The terminal proxy picked its settings file by the version of the CLI
        it started and said so; a CLI with no proxy got the file without it.
        """
        link = self.hub.ptys.get(state.attachment.pid)
        return link is not None and link.tool_batch

    @staticmethod
    def _owes_turn_end(entry: SessionEntry, state: SharedState) -> bool:
        """Whether a turn's end has anything to write: a steer, or a held message."""
        if state.question is not None or state.busy_typing:
            return False
        return bool(state.steers or entry.queue)

    async def _tool_boundary(
        self, entry: SessionEntry, state: SharedState, deadline: float
    ) -> dict[str, str]:
        """Hand the model everything steered into this turn, in the order it was sent.

        A prompt the person queued in the terminal is folded in at the same
        boundary, and where the model reads it beside a hook's context depends
        on the CLI, not on when it was typed: holding a steer back for the next
        boundary would only delay it. The bubbles wait for the transcript,
        which tells that order (`steer_delivered`). Messages whose files took
        the hook past its deadline stay steers for the next boundary. A batch
        is a turn under way, however it began.
        """
        state.prompted_at = time.monotonic()
        prompts: list[tuple[str, str]] = []
        handed: list[dict[str, Any]] = []
        for item in list(state.steers):
            written = await self._files(entry, item)
            if written is None:
                continue
            words = attachments.describe(str(item["text"]), written)
            prompts.append((str(item["message_id"]), words))
            handed.append(item)
        if not prompts or time.time() > deadline - REPLY_MARGIN:
            return {}
        for item in handed:
            state.steers.remove(item)
            state.handed[str(item["message_id"])] = item
        return {"context": steer_context(prompts), "system_message": steer_note(handed)}

    @staticmethod
    def _turn_over(state: SharedState) -> None:
        """The CLI says its turn is over, for injecting; the apps hear it from the transcript.

        Ending the turn here would end it too early: somebody's own `Stop` hook
        can send it on, and only the transcript says how it ended (A32, A35).
        """
        state.prompted_at = None
        state.command_at = None
        state.running = False

    async def _turn_end(self, entry: SessionEntry, state: SharedState, deadline: float) -> None:
        """Write what waits for this turn's end into the channel before the CLI idles.

        The `Stop` hook holds the CLI until the bridge has written it, so the
        CLI starts it as the next turn, never folded into the one ending, and
        it is drawn when that turn starts (`echo_delivered`).
        """
        items, took = self._turn_end_batch(entry, state)
        if not items:
            return
        state.stopping = True
        state.turn_at_stop = entry.session.turn
        try:
            written = await self._deliver(entry, items, took, at_stop=True)
            budget = min(STOP_WRITE_TIMEOUT, deadline - REPLY_MARGIN - time.time())
            if written and budget > 0:
                await state.attachment.landed(written, budget)
        finally:
            state.stopping = False

    @staticmethod
    def _turn_end_batch(
        entry: SessionEntry, state: SharedState
    ) -> tuple[list[dict[str, Any]], bool]:
        """What goes in at a turn's end, oldest first, and whether the queue gave any.

        Every steer the turn had no boundary left for goes, and with them the
        oldest held message when it was sent before them; with no steers, the
        oldest held message alone, one per turn's end (A19, A43).
        """
        steers = sorted(state.steers, key=_sent_order)
        state.steers.clear()
        head = entry.queue[0] if entry.queue else None
        take = head is not None and (not steers or _sent_order(head) < _sent_order(steers[0]))
        if head is not None and take:
            entry.queue.pop(0)
            steers.insert(0, head)
        return steers, take

    # ----------------------------------------------------------------- input

    async def send(
        self,
        entry: SessionEntry,
        request_id: str,
        text: str,
        uploads: list[dict[str, Any]],
        source: str = "remote",
        queue_ts: int | None = None,
        mode: str = "auto",
    ) -> dict[str, Any]:
        state = entry.shared
        if state is None:
            raise RcError("conflict", "the session is no longer attached")
        # A49: the files are written first, so a message refused for a bad one
        # changes nothing, and they are there whenever the CLI reads its words.
        written = await attachments.store(entry.session.session_id, uploads)
        await self._announce(entry, text, source, written)
        item = pending_item(text, request_id, source, uploads, written, mode)
        if (entry.queue or state.steers) and state.injectable:
            # A turn's end drains from a task of its own, so a message the turn
            # held may not have gone yet: it goes first, and this one after it.
            await self.drain_locked(entry)
        if state.injectable and not entry.queue and not state.steers:
            if await self._inject(entry, item):
                return {"accepted": "sent"}
            # A bridge refuses only as it goes away; whatever delivers the
            # message next writes its files again from the bytes it kept (A49).
            await release(item)
        # Amendment A19: held is not delivered, and not a block either. The
        # bubble is drawn when the CLI takes the message, which is where the
        # terminal shows it too. An edited message is held where it was (A43).
        hold(entry.queue, item, queue_ts)
        await entry.channel.publish_queue(self.hub.queue_snapshot(entry))
        return {"accepted": "queued", "queued_id": str(item["id"])}

    async def steer(
        self,
        entry: SessionEntry,
        request_id: str,
        text: str,
        uploads: list[dict[str, Any]],
        source: str = "remote",
    ) -> bool:
        """Take a message into the running turn, to be handed over at its next step (A51).

        False, with nothing written, when the CLI runs no turn hooks or no turn:
        the message is then sent as any other is. A steered message is no queue
        entry and no block until the model has it, as A14 has it for Codex.
        """
        state = entry.shared
        if state is None or not state.hooked or not state.busy:
            return False
        written = await attachments.store(entry.session.session_id, uploads)
        await self._announce(entry, text, source, written)
        state.steers.append(pending_item(text, request_id, source, uploads, written, "auto"))
        return True

    async def _announce(
        self,
        entry: SessionEntry,
        text: str,
        source: str,
        written: list[attachments.Attachment],
    ) -> None:
        """A message is on its way: the session is live, and named after it if it was not."""
        try:
            # Amendment A15: held or injected, the message brings the session back.
            await entry.channel.revive()
            if source != "resume":
                # The resume prompt is the device's sentence, not a title (A35).
                await titles.from_prompt(entry.channel, text, attachments.wire_attachments(written))
        except Exception:
            await attachments.drop(written)
            raise

    async def drain_locked(self, entry: SessionEntry) -> None:
        """Release what waited once the transcript says the turn ended.

        The hub's drain task calls this holding the session's lock, as a send
        does: a second drain, or a message sent meanwhile, waits until this one
        is in the CLI and counts as in flight, so nothing overtakes it and
        nothing is injected into the turn it starts while its files are
        written (A19, A43). It is the turn's end a CLI without the turn hooks
        has, and the one a turn ended without `Stop` has (A51).
        """
        state = entry.shared
        if state is None or not state.injectable or not (entry.queue or state.steers):
            return
        state.inflight = None
        items, took = self._turn_end_batch(entry, state)
        await self._deliver(entry, items, took)

    async def _deliver(
        self, entry: SessionEntry, items: list[dict[str, Any]], took: bool, at_stop: bool = False
    ) -> list[str]:
        """Inject these in order, and say which went; the bridge refusing stops the rest.

        A message it refused goes back under its own `ts` (A43), with whatever
        was to follow it, and their files with them: the next attempt writes
        them again (A49). A queue that changed is published once, at the end.
        """
        sent: list[str] = []
        for position, item in enumerate(items):
            if await self._inject(entry, item, at_stop):
                sent.append(str(item["message_id"]))
                continue
            for rest in items[position:]:
                await release(rest)
                hold(entry.queue, rest, int(rest["ts"]))
            took = True
            break
        if took:
            await entry.channel.publish_queue(self.hub.queue_snapshot(entry))
        return sent

    async def _inject(
        self, entry: SessionEntry, item: dict[str, Any], at_stop: bool = False
    ) -> bool:
        """Write one message into the channel; `at_stop`, for the turn after this one.

        Written at a turn's end, it is drawn when its row shows, after the turn
        it follows (`echo_delivered`). So is one written while the CLI holds an
        earlier message (`_behind`), which it takes first: the bubbles keep the
        order the CLI takes them in, and the message is one written at a
        turn's end in every other way too. Written into an idle CLI holding
        nothing, it is drawn now, and starts the turn the apps see (A19, A51).
        """
        state = entry.shared
        if state is None:
            return False
        written = await self._files(entry, item)
        if written is None:
            return False
        message_id = str(item["message_id"])
        prompt = attachments.describe(str(item["text"]), written)
        at_stop = at_stop or self._behind(entry, state)
        if not await state.attachment.inject(message_id, prompt):
            return False
        state.mark_inflight(message_id, at_stop)
        remember(entry, item)
        if at_stop:
            item["at_stop"] = True
            return True
        state.trigger = str(item.get("source") or "remote")
        await self._emit_message(entry, item, "delivered")
        await self._settle(entry)
        return True

    @staticmethod
    def _behind(entry: SessionEntry, state: SharedState) -> bool:
        """Whether the CLI holds, or may hold, an earlier message this one goes in after (A51).

        One it queued and has not started — a slow `Stop` hook of somebody's
        own or a dialog holding its queue — or one written for a turn's end
        whose row has not shown yet.
        """
        return bool(state.held_by_cli) or any(item.get("at_stop") for item in entry.sent.values())

    @staticmethod
    async def _files(
        entry: SessionEntry, item: dict[str, Any]
    ) -> list[attachments.Attachment] | None:
        """The files a held message's words name, written again if they are gone.

        A detachment takes them on purpose, and the size cap may have taken
        them while the message waited; the bytes it kept write them again.
        `None` means they could not be: the message stays held and is tried at
        the next idle point, like one the bridge refused.
        """
        written = item.get("written")
        if written is not None and not written:
            return []
        if written and await asyncio.to_thread(attachments.present, written):
            return list(written)
        await release(item)
        uploads = list(item.get("attachments") or [])
        try:
            item["written"] = await attachments.store(entry.session.session_id, uploads)
        except RcError:
            log.warning("a held message's attachments could not be written again")
            return None
        return list(item["written"])

    async def _emit_message(self, entry: SessionEntry, item: dict[str, Any], delivery: str) -> None:
        """The bubble: the words as typed and the files as names, never their paths.

        `shown` keeps the delivery last drawn, so a message drawn delivered is
        never drawn so again, and one drawn absorbed is when it goes in (A51).
        """
        item["shown"] = delivery
        written = item.get("written") or []
        await entry.channel.emit(
            "user_message",
            block_id=str(item["block_id"]),
            text=str(item["text"]),
            source=str(item.get("source") or "remote"),
            delivery=delivery,
            **({"attachments": attachments.wire_attachments(written)} if written else {}),
        )

    def steering(self, entry: SessionEntry, source: str) -> SharedSteering:
        """This session as the hub steers it (A51)."""
        return SharedSteering(control=self, entry=entry, source=source)

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
            pending.resolve(None)

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
        pending.resolve(None)

    def read_soon(self) -> None:
        """Have the transcript reader make its next pass now (A40).

        A command the device typed is confirmed by the rows it leaves, and
        waiting for the reader's next pass on its own, or for the scan that
        finds a transcript a fresh session has only just started, took longer
        than the confirmation waits.
        """
        if self.reader is not None:
            self.reader.read_soon()

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
        said = text.lower()
        pending.resolve(said if pending.confirms in said else None)

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
        if reads_session_file(entry.session.session_id, tool, previewed_input(preview)):
            # A49: an attachment the injected words named, which is the
            # session's own; the terminal's dialog closes with the answer.
            await state.attachment.relay_permission(channel_id, ALLOW)
            return
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
