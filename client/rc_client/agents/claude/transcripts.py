"""Mirror Claude sessions started in a terminal by tailing their transcripts.

Growth is detected by `st_size`, never `st_mtime`: `claude --resume` touches
mtime without appending a byte, which would otherwise look like live activity.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...attachments import split_prompt
from ...channel.versions import version_numbers
from ...diffs import from_tool_input
from ...models import now_ms
from ...sessions.limits import LimitStop, claude_row_limit
from ...tailing import FileTail
from ..base import COMPACTION_NOTICE, Emit
from . import markers
from .injected import classify, envelope, from_agent, strip_reminders
from .questions import QUESTION_TOOL
from .runtime import PROJECTS_DIR
from .tools import todos_from_input, tool_kind, tool_title

MAX_TRANSCRIPTS = 50
MAX_AGE_DAYS = 14
# Tags a typed command leaves behind that say nothing to a reader: the caveat
# is an instruction to the model, and the other two only repeat the command's
# own name and argument, which `markers.typed_command` already read.
_SKIP_PREFIXES = ("<local-command-caveat>", "<command-message>", "<command-args>")
_SKIP_TEXTS = {"No response requested.", "(no content)"}

# Amendment A10: a message this device injected through the channel comes back
# wrapped in a <channel> tag whose attributes repeat the `meta` we sent, so the
# id we chose is the correlation key. Neither shape becomes a second bubble.
CHANNEL_SERVER = "rc"
_MESSAGE_ID_RE = re.compile(r'message_id="([^"]{1,64})"')
CHANNEL_DELIVERED = "channel_delivered"
CHANNEL_ABSORBED = "channel_absorbed"

# Amendment A51: the CLI logs its own command queue in the transcript, so an
# injection it has queued but not started is seen there; and a message a turn
# hook handed the model at a tool boundary comes back as that hook's additional
# context, still tagged with its id. What the CLI folds into a running turn at
# a tool boundary — the person's prompt queued in the terminal, another agent's
# message, a task's notification — comes as a `queued_command` attachment; of
# its modes, only these two are words.
CHANNEL_QUEUED = "channel_queued"
CHANNEL_DROPPED = "channel_dropped"
CHANNEL_PULLED = "channel_pulled"
QUEUE_MOVED = "queue_moved"
STEER_DELIVERED = "steer_delivered"
QUEUE_OPERATION_ROW = "queue-operation"
HOOK_CONTEXT = "hook_additional_context"
QUEUED_COMMAND = "queued_command"
_FOLDED_WORDS = frozenset({"prompt", "task-notification"})
# How the CLI's queue lets a message go without running it: removed, which
# absorbing it is one kind of, or pulled back into the input box to be edited.
_PULLED_OPERATIONS = frozenset({"popAll", "popOne"})
ABSORBED_MID_TURN = "absorbed_mid_turn"
# What the CLI writes when somebody's `Stop` hook sends a finished turn on, and
# the tag every row of a typed slash command carries, whatever comes first.
STOP_FEEDBACK = "Stop hook feedback:"
_COMMAND_ROW = re.compile(r"<command-name>[^<]+</command-name>")
# The first Claude Code seen to put what it folds in ahead of a hook's context
# at one boundary; 2.1.291 puts it after, which is the order its rows come in.
FOLDS_FIRST_VERSION = (2, 1, 294)
_CHANNEL_TAG_ID = re.compile(r'<channel source="rc"[^>]{0,200}?message_id="([^"]{1,64})"')

# Amendment A40: what the CLI printed back after a typed command. It is never
# published — it carries terminal escapes no app can show — but it is the only
# record that a command the device typed actually did anything, so it is
# reported as a signal of its own rather than dropped where it is read.
COMMAND_OUTPUT = "command_output"

# Amendment A20: the result row of an `AskUserQuestion` is how the device hears
# that the person answered the CLI's own dialog. `toolUseResult` carries what
# they chose, under the question's own prompt.
QUESTION_ANSWERED = "question_answered"

# Claude Code names a session after its own reading of the conversation and
# writes the result into the transcript as a row of its own. It may do so more
# than once — the title is generated shortly after the first turn, and accepting
# a plan replaces it — and the last one is the current title. `/rename` in the
# terminal writes the other shape, which is the user's own title and outranks
# every generated one. Both are internal to Claude Code and may change, so an
# unknown or malformed row is read as "not a title" rather than as an error.
AI_TITLE_ROW = "ai-title"
CUSTOM_TITLE_ROW = "custom-title"
TITLE = "session_title"
_TITLE_ROWS = {AI_TITLE_ROW: ("aiTitle", False), CUSTOM_TITLE_ROW: ("customTitle", True)}
# `/rename` writes the name the prompt bar shows after a resume as a row of its
# own (A55). Read by nobody here: the two rows together are a rename.
AGENT_NAME_ROW = "agent-name"
_NAME_ROWS = frozenset({CUSTOM_TITLE_ROW, AGENT_NAME_ROW})
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")

# Amendment A17: a session the terminal holds takes no settings from an app, so
# the device reads what the terminal chose out of the transcript and publishes
# it as `meta`. Claude Code writes the model as an attachment at session start,
# after a resume and on every `/model`; the permission mode as a row of its own
# once per turn; and the effort on each assistant message. All three are
# internal to Claude Code, so an unknown or malformed row reads as "no
# settings" rather than as an error, exactly as the title rows do.
SESSION_SETTINGS = "session_settings"
PERMISSION_MODE_ROW = "permission-mode"
MODEL_ATTACHMENT = "model"
# One bounded pass over a transcript is enough to find the values in force; the
# cap keeps a runaway file from holding the thread it runs on.
SETTINGS_SCAN_BYTES = 64 * 1024 * 1024


def channel_message_id(text: str, server: str = CHANNEL_SERVER) -> str | None:
    """The `message_id` of a channel tag this device wrote, if that is what it is."""
    head = text[:400]
    if not head.lstrip().startswith(f'<channel source="{server}"'):
        return None
    match = _MESSAGE_ID_RE.search(head)
    return match.group(1) if match else None


def _channel_origin(row: dict[str, Any], server: str = CHANNEL_SERVER) -> bool:
    origin = row.get("origin")
    if not isinstance(origin, dict):
        return False
    return origin.get("kind") == "channel" and origin.get("server") == server


@dataclass(slots=True)
class TranscriptInfo:
    session_id: str
    path: str
    cwd: str
    size: int
    mtime: float


def _first_row(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("cwd"):
                    return row
    except OSError:
        return None
    return None


def discover(
    limit: int = MAX_TRANSCRIPTS, max_age_days: int = MAX_AGE_DAYS
) -> list[TranscriptInfo]:
    """The most recent top-level transcripts; subagent files live one level deeper.

    Both bounds matter on a developer machine: without them a freshly enrolled
    device would publish every session the user has ever run.
    """
    root = PROJECTS_DIR
    if not root.is_dir():
        return []
    cutoff = time.time() - max_age_days * 86400
    candidates: list[tuple[float, Path]] = []
    for project in root.iterdir():
        if not project.is_dir():
            continue
        for entry in project.glob("*.jsonl"):
            try:
                stat = entry.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff or stat.st_size == 0:
                continue
            candidates.append((stat.st_mtime, entry))
    candidates.sort(reverse=True)
    found: list[TranscriptInfo] = []
    for mtime, path in candidates[:limit]:
        info = _info(path, mtime)
        if info is not None:
            found.append(info)
    return found


def _info(path: Path, mtime: float) -> TranscriptInfo | None:
    row = _first_row(path)
    if row is None:
        return None
    return TranscriptInfo(
        session_id=str(row.get("sessionId") or path.stem),
        path=str(path),
        cwd=str(row.get("cwd") or ""),
        size=path.stat().st_size,
        mtime=mtime,
    )


def find_transcript(session_id: str) -> Path | None:
    """The transcript of one session, in whichever project directory Claude filed it.

    A session this device drives is never mirrored, so its path is not known
    from a scan; the id is unique across projects, which makes a glob enough.
    """
    if not _SESSION_ID.fullmatch(session_id):
        return None
    for path in PROJECTS_DIR.glob(f"*/{session_id}.jsonl"):
        return path
    return None


def find_info(session_id: str) -> TranscriptInfo | None:
    """One session's transcript as `discover` describes it, once there is one to read."""
    path = find_transcript(session_id)
    if path is None:
        return None
    try:
        return _info(path, path.stat().st_mtime)
    except OSError:
        return None


def is_name_row(row: dict[str, Any]) -> bool:
    """Whether the row only records a name the user gave the session (A55)."""
    return row.get("type") in _NAME_ROWS


# How long the rows that follow a rename's command row still count as its own
# when they are read in a later pass than the command's.
NAME_ROWS_WINDOW = 10.0


@dataclass(slots=True)
class NameRows:
    """Tells the rows a rename leaves from every other row, across reads (A55).

    A rename is not activity, whoever typed it, so a batch of nothing but its own
    rows does not move the session up the list. Its own rows are the two naming
    rows, `custom-title` and `agent-name`; its command row; and the hidden rows
    that come with the command: what the CLI printed back, and the note it
    leaves for the model. Those two are told by where they stand — touching a
    rename's command row, on either side — because nothing in them says what
    command they belong to, and the CLI's wording is not something to read. Any
    other row in the batch is activity, as it always was.
    """

    until: float = 0.0

    def only(self, rows: Sequence[dict[str, Any]], now: float | None = None) -> bool:
        """Whether every row is a rename's own; what follows a command carries over."""
        now = time.monotonic() if now is None else now
        kinds = [_row_kind(row) for row in rows]
        own = [kind in ("name", "command") for kind in kinds]
        attached = now < self.until
        for index, kind in enumerate(kinds):
            if kind == "command":
                attached = True
            elif kind in ("hidden", "output"):
                own[index] = own[index] or attached
            elif kind != "name":
                attached = False
        self.until = now + NAME_ROWS_WINDOW if attached else 0.0
        following = False
        for index in reversed(range(len(kinds))):
            kind = kinds[index]
            if kind == "command":
                following = True
            elif kind in ("hidden", "output"):
                own[index] = own[index] or following
            elif kind != "name":
                following = False
        return all(own)


def _row_kind(row: dict[str, Any]) -> str:
    """`name`, `command` (a typed rename), `hidden`, `output` (a command's answer) or `other`."""
    if is_name_row(row):
        return "name"
    if row.get("type") != "user":
        return "other"
    if row.get("isMeta"):
        return "hidden"
    message = row.get("message")
    text = _text_of(message.get("content")) if isinstance(message, dict) else ""
    if markers.is_rename(text):
        return "command"
    return "output" if markers.is_command_output(text) else "other"


@dataclass(slots=True, frozen=True)
class Title:
    """A title read from a transcript, and whether the user chose it."""

    text: str
    by_user: bool


def read_title(row: dict[str, Any]) -> Title | None:
    """The title a transcript row carries, or `None` when it carries none."""
    shape = _TITLE_ROWS.get(str(row.get("type") or ""))
    if shape is None:
        return None
    field_name, by_user = shape
    value = row.get(field_name)
    text = value.strip() if isinstance(value, str) else ""
    return Title(text=text, by_user=by_user) if text else None


@dataclass(slots=True)
class TitleTail:
    """Watch a transcript for nothing but the titles it carries.

    Sessions this device drives get their events from the SDK, which does not
    carry the title, and mirroring their transcript would publish every message
    twice. Reading only the title rows costs one incremental read per tick.
    """

    path: str
    tail: FileTail = field(init=False)

    def __post_init__(self) -> None:
        self.tail = FileTail(path=self.path)

    def read_new(self) -> list[Title]:
        """Every title among the rows appended since the last read, in order.

        All of them, not just the last: a generated title that lands after a
        rename must not win, and applying them in order is what decides that.
        """
        found = [read_title(row) for row in self.tail.read_new()]
        return [title for title in found if title is not None]


def _setting(name: str, value: Any) -> dict[str, str]:
    text = value.strip() if isinstance(value, str) else ""
    return {name: text} if text else {}


def _model_setting(row: dict[str, Any]) -> dict[str, str]:
    attachment = row.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != MODEL_ATTACHMENT:
        return {}
    identity = attachment.get("identity")
    if not isinstance(identity, dict):
        return {}
    # The id is kept verbatim, suffix and all: `claude-opus-5[1m]` is a model an
    # app must be able to show even though no agent list carries it.
    return _setting("model", identity.get("modelId"))


def read_settings(row: dict[str, Any]) -> dict[str, str]:
    """The session settings a transcript row carries, empty when it carries none."""
    row_type = row.get("type")
    if row_type == "attachment":
        return _model_setting(row)
    if row_type == PERMISSION_MODE_ROW:
        return _setting("permission_mode", row.get("permissionMode"))
    if row_type == "assistant":
        # `perTurnEffort` is a different field on other rows and is not this one.
        return _setting("effort", row.get("effort"))
    return {}


def latest_settings(path: str | Path, limit: int = SETTINGS_SCAN_BYTES) -> dict[str, str]:
    """The settings in force at the end of a transcript, in one streaming pass.

    A mirror starts reading at a stored offset or near the end of the file, and
    the model is recorded only when it changes, so the value in force usually
    lies far behind that point. Reading the whole file once, when the session is
    adopted, is what lets an app show the right values from the first frame.
    """
    found: dict[str, str] = {}
    read = 0
    try:
        with open(path, "rb") as handle:
            for line in handle:
                read += len(line)
                if read > limit:
                    break
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # Not JSON, or not even UTF-8: a row cut short or written
                    # over says nothing about the settings the others record.
                    continue
                if isinstance(row, dict):
                    found.update(read_settings(row))
    except OSError:
        return found
    return found


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    return ""


def _at_boundary(row: dict[str, Any]) -> bool:
    """A row a tool boundary writes around what it hands the model (A51).

    Its hooks' rows, what it folded into the turn, and the queue moving; the
    next row of any other kind is the model's next step.
    """
    if row.get("type") == QUEUE_OPERATION_ROW:
        return True
    attachment = row.get("attachment") if row.get("type") == "attachment" else None
    kind = attachment.get("type") if isinstance(attachment, dict) else None
    return isinstance(kind, str) and (kind == QUEUED_COMMAND or kind.startswith("hook_"))


def _folds_first(row: dict[str, Any]) -> bool:
    """Whether the model reads what a boundary folded in before the hook's context (A51).

    Each row names the CLI that wrote it; one that does not is taken to be
    recent.
    """
    numbers = version_numbers(str(row.get("version") or ""))
    return numbers is None or numbers >= FOLDS_FIRST_VERSION


def _is_meta(row: dict[str, Any], text: str) -> bool:
    if row.get("isMeta"):
        return True
    stripped = text.strip()
    if stripped in _SKIP_TEXTS:
        return True
    return any(stripped.startswith(prefix) for prefix in _SKIP_PREFIXES)


@dataclass(slots=True)
class TranscriptTailer:
    """Incremental JSONL reader that converts new rows into session events."""

    path: str
    cwd: str
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    awaiting_reply: bool = False
    # What started the turn the rows read so far leave open (amendment A30):
    # the person at the keyboard, this device's own injection, or another agent
    # whose words the CLI filed as a user turn.
    turn_trigger: str = "terminal"
    # How that turn ended (amendment A32). Only the marker the CLI leaves where
    # the person stopped a turn makes it anything but `completed`, and the next
    # turn to start clears it.
    stop_reason: str = "completed"
    # The text of the last user message published, which is how the second
    # record the CLI keeps of one typed command is recognised (amendment A32).
    last_message: str = ""
    # The vendor's usage limit that ended the turn, when one did (amendment
    # A35). Set beside `stop_reason = "error"`, and cleared by the next turn.
    limit: LimitStop | None = None
    tail: FileTail = field(init=False)
    # Steered messages a tool boundary's hook handed the model (A51), confirmed
    # once that boundary's rows are read, after what it folded into the turn
    # when the model reads that first.
    steered: list[str] = field(default_factory=list, init=False)
    # A slash command was just typed: the hidden row that follows it, if any,
    # is what the command expands to, and the turn it starts (A51).
    expanding: bool = field(default=False, init=False)
    # Which of the rows read were only a rename's own (A55).
    names: NameRows = field(default_factory=NameRows, init=False)

    def __post_init__(self) -> None:
        self.tail = FileTail(path=self.path)

    @property
    def offset(self) -> int:
        return self.tail.offset

    @offset.setter
    def offset(self, value: int) -> None:
        self.tail.offset = value

    @property
    def busy(self) -> bool:
        """Whether a turn is in progress, as the rows read so far leave it."""
        return self.awaiting_reply

    def seek_to_end(self) -> None:
        self.tail.seek_to_end()

    def read_new(self) -> list[dict[str, Any]]:
        return self.tail.read_new()

    def only_names(self, rows: Sequence[dict[str, Any]]) -> bool:
        """Whether the rows read are nothing but a rename's own, which is not activity (A55)."""
        return bool(rows) and self.names.only(rows)

    def translate(self, row: dict[str, Any]) -> list[Emit]:
        settings = read_settings(row)
        emits: list[Emit] = [Emit(SESSION_SETTINGS, dict(settings))] if settings else []
        if self.steered and not _at_boundary(row):
            emits.append(Emit(STEER_DELIVERED, {"message_ids": self.steered}))
            self.steered = []
        return emits + self._content(row)

    def _content(self, row: dict[str, Any]) -> list[Emit]:
        row_type = row.get("type")
        if row_type in _TITLE_ROWS:
            title = read_title(row)
            if title is None:
                return []
            return [Emit(TITLE, {"title": title.text, "by_user": title.by_user})]
        if row_type == "user":
            return self._channel_echo(row) or self._user(row)
        if row_type == "assistant":
            return self._assistant(row)
        if row_type == "attachment":
            return self._attachment(row)
        if row_type == "system":
            return self._system(row)
        if row_type == QUEUE_OPERATION_ROW:
            return self._queue_operation(row)
        return []

    @staticmethod
    def _queue_operation(row: dict[str, Any]) -> list[Emit]:
        """An injection the CLI has queued and not yet started, or let go (A51).

        Its queue does not move while a dialog or a panel is open, so the
        message may wait there; the row of its own turn, of its being absorbed,
        or of its leaving the queue without running says what became of it.
        One row may name several messages — `popAll` takes the whole queue
        back at once — and it speaks for each of them, wherever it names them.
        """
        moved = [Emit(QUEUE_MOVED, {})]
        operation = row.get("operation")
        if operation == "enqueue":
            kind = CHANNEL_QUEUED
        elif operation == "remove" and row.get("reason") != ABSORBED_MID_TURN:
            kind = CHANNEL_DROPPED
        elif operation in _PULLED_OPERATIONS:
            kind = CHANNEL_PULLED
        else:
            return moved
        content = str(row.get("content") or "")
        named = [found.group(1) for found in _CHANNEL_TAG_ID.finditer(content)]
        return [*(Emit(kind, {"message_id": message_id}) for message_id in named), *moved]

    def _system(self, row: dict[str, Any]) -> list[Emit]:
        """The one system row worth publishing: where the context was compacted.

        Amendment A32: the summary that follows is not a block, so this line is
        all an app hears about a compaction, exactly as it does for Codex.
        """
        if not markers.is_compact_boundary(row):
            return []
        return [Emit("notice", {"level": "info", "text": COMPACTION_NOTICE})]

    def _channel_echo(self, row: dict[str, Any]) -> list[Emit]:
        """Injected messages reappearing as a user row: a turn just started.

        The CLI may file several of them in one row, each in its own tag, and
        every one is confirmed.
        """
        if not _channel_origin(row):
            return []
        message = row.get("message") or {}
        text = _text_of(message.get("content"))
        if channel_message_id(text) is None:
            return []
        self._begin_turn("remote")
        ids = [found.group(1) for found in _CHANNEL_TAG_ID.finditer(text)]
        return [Emit(CHANNEL_DELIVERED, {"message_id": message_id}) for message_id in ids]

    def _attachment(self, row: dict[str, Any]) -> list[Emit]:
        """What a tool boundary hands the model: a hook's context, or what it folded in."""
        attachment = row.get("attachment")
        if not isinstance(attachment, dict):
            return []
        if attachment.get("type") == HOOK_CONTEXT:
            handed = _handed_over(attachment)
            if _folds_first(row):
                self.steered.extend(handed)
                return []
            return [Emit(STEER_DELIVERED, {"message_ids": handed})] if handed else []
        if attachment.get("type") != QUEUED_COMMAND:
            return []
        return self._folded(row, attachment)

    def _folded(self, row: dict[str, Any], attachment: dict[str, Any]) -> list[Emit]:
        """A message the CLI folded into the running turn at a tool boundary (A51).

        Our own injection folded this way was absorbed, and is no bubble. Any
        other is published as A30 publishes the same words when they start a
        turn — the person's as `terminal`, another agent's envelope as
        `agent`, nothing for what such a row would not show — but it starts
        nothing: the turn it joined is already running.
        """
        prompt = attachment.get("prompt")
        if _channel_origin(attachment):
            message_id = channel_message_id(str(prompt or ""))
            return [Emit(CHANNEL_ABSORBED, {"message_id": message_id})] if message_id else []
        if attachment.get("commandMode", "prompt") not in _FOLDED_WORDS:
            return []
        text = _text_of(prompt)
        if _is_meta(attachment, text):
            hidden = attachment.get("isMeta") and from_agent(attachment)
            reported = envelope(text) if hidden else None
            return self._publish(row, reported, "agent") if reported else []
        message = classify(attachment, text)
        if message is None:
            return []
        return self._publish(row, message.text, "agent" if message.by_agent else "terminal")

    def _user(self, row: dict[str, Any]) -> list[Emit]:
        message = row.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return self._message(row, content)
        emits: list[Emit] = []
        text_parts: list[str] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                emits.extend(self._tool_result(block, row))
            elif block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        return emits + self._message(row, "\n".join(part for part in text_parts if part))

    def _message(self, row: dict[str, Any], text: str) -> list[Emit]:
        """Publish a user row as whosever words it holds, if it holds any.

        A teammate's message and a task's notification are user turns nobody
        typed, so they are published as `agent` and start their turn as one
        (amendment A30). The rows of amendment A32 are not words at all: the
        summary of a compaction, the marker of an interruption, the CLI's reply
        to a command. A row left empty once the CLI's own reminders come off is
        not a message either.
        """
        expanding, self.expanding = self.expanding, False
        if not row.get("isMeta") and _COMMAND_ROW.search(text):
            # What a command expands to is a turn, but the note a rename leaves
            # for the model is not: nothing answers it (A55).
            self.expanding = not markers.is_rename(text)
        if _is_meta(row, text):
            return self._hidden_turn(row, text, expanding)
        said = strip_reminders(text)
        if not said:
            return []
        if markers.is_compaction_summary(row):
            return []
        if markers.is_interruption(said):
            # The person stopped the turn: it ends here, as interrupted, and
            # the tool result this row may also carry is published as usual.
            self.awaiting_reply = False
            self.stop_reason = "interrupted"
            self.limit = None
            return []
        if markers.is_command_output(said):
            # The CLI answered the command, which is the end of what it did —
            # and the end of the keystroke the next tag could be a repeat of,
            # so the same command typed again is a message of its own. The
            # answer itself is never published, only reported (A40).
            self.awaiting_reply = False
            self.last_message = ""
            return [Emit(COMMAND_OUTPUT, {"text": said})]
        command = markers.typed_command(said)
        if command is not None:
            return self._command(row, command)
        return self._words(row, said)

    def _hidden_turn(self, row: dict[str, Any], text: str, expanding: bool) -> list[Emit]:
        """A row the CLI hides that starts a turn all the same, or sends one on (A51).

        Claude Code files a teammate's report as `isMeta` under `origin.kind:
        "peer"`, and goes on by itself under `auto-continuation`; the model's
        turn starts there as it does for any prompt, and a device that missed
        it injected into the running turn. A teammate's or a task's envelope is
        published as A30 says; anything else starts the turn without a block.
        So does what a slash command expands to — a skill, a command of the
        person's — right after the command's row, and the feedback of
        somebody's `Stop` hook sends the turn that was ending on.
        """
        if not row.get("isMeta"):
            return []
        said = strip_reminders(text)
        if expanding and said and not said.startswith(_SKIP_PREFIXES):
            if not markers.is_compaction_summary(row):
                self._begin_turn("terminal")
            return []
        if said.startswith(STOP_FEEDBACK):
            self._begin_turn(self.turn_trigger)
            return []
        if not from_agent(row):
            return []
        reported = envelope(text)
        self._begin_turn("agent")
        return self._publish(row, reported, "agent") if reported else []

    def _command(self, row: dict[str, Any], command: str) -> list[Emit]:
        """A slash command typed at the terminal: the person's, but not a turn.

        The CLI records `/compact` twice — once as the keystroke, once as this
        tag — so the record that repeats the message just published is dropped
        rather than drawn as a second bubble.
        """
        if command == self.last_message:
            return []
        return self._publish(row, command, "terminal")

    def _words(self, row: dict[str, Any], said: str) -> list[Emit]:
        """A prompt or an agent's message: it starts a turn and opens a bubble."""
        message = classify(row, said)
        if message is None:
            return []
        source = "agent" if message.by_agent else "terminal"
        self._begin_turn(source)
        return self._publish(row, message.text, source)

    def _begin_turn(self, trigger: str) -> None:
        """A turn starts here, which clears how the last one ended."""
        self.awaiting_reply = True
        self.turn_trigger = trigger
        self.stop_reason = "completed"
        self.limit = None

    def _publish(self, row: dict[str, Any], text: str, source: str) -> list[Emit]:
        """The bubble, with the paths of a device's attachments taken off the words (A49)."""
        self.last_message = text
        said, files = split_prompt(text)
        fields: dict[str, Any] = {
            "block_id": str(row.get("uuid") or f"user:{now_ms()}"),
            "text": said,
            "source": source,
        }
        if files:
            fields["attachments"] = files
        return [Emit("user_message", fields)]

    def _assistant(self, row: dict[str, Any]) -> list[Emit]:
        """One content block of one assistant message, and whether it ended the turn.

        Claude Code files each block of a message as a row of its own, and every
        one of them carries the message's final `stop_reason`, so a row holding
        only the sentence the model says before its tool calls looks exactly
        like the last row of a turn. The CLI's own word settles it (amendment
        A34): `tool_use` means the turn goes on whatever the row holds, and
        anything else — `end_turn`, `stop_sequence`, `max_tokens`, or none at
        all — ends it. Reading the blocks instead once drained a phone's held
        messages into a turn that was still running.
        """
        self.expanding = False
        limited = self._limit_row(row)
        if limited is not None:
            return limited
        message = row.get("message") or {}
        message_id = str(message.get("id") or row.get("uuid") or "msg")
        first = _first_block_index(row)
        emits: list[Emit] = []
        has_tool = False
        for position, block in enumerate(message.get("content") or []):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            block_id = f"{message_id}:{first + position}"
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    emits.append(
                        Emit("assistant_text", {"block_id": block_id, "text": text, "done": True})
                    )
            elif block_type == "thinking":
                text = str(block.get("thinking") or "")
                if text:
                    emits.append(
                        Emit("thinking", {"block_id": block_id, "text": text, "done": True})
                    )
            elif block_type == "tool_use":
                has_tool = True
                emits.extend(self._tool_use(block))
        if not has_tool and message.get("stop_reason") != "tool_use":
            self.awaiting_reply = False
        elif not self.awaiting_reply:
            # A turn the rows never showed starting is under way all the same.
            self._begin_turn(self.turn_trigger)
        return emits

    def _limit_row(self, row: dict[str, Any]) -> list[Emit] | None:
        """The 429 row the CLI writes when the vendor's usage window is used up.

        Amendment A35: this is not the agent speaking, so its sentence goes out
        as an `error` rather than as `assistant_text`, and the turn it ends is
        an `error` stop carrying the window and its reset.
        """
        limit = claude_row_limit(row)
        if limit is None:
            return None
        self.awaiting_reply = False
        self.stop_reason = "error"
        self.limit = limit
        message = row.get("message") or {}
        said = _text_of(message.get("content")).strip()
        return [Emit("error", {"message": said})] if said else []

    def _tool_use(self, block: dict[str, Any]) -> list[Emit]:
        name = str(block.get("name") or "Tool")
        raw_input = block.get("input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        block_id = str(block.get("id") or "")
        if not block_id:
            return []
        started_at = now_ms()
        self.tools[block_id] = {"tool": name, "input": tool_input, "started_at": started_at}
        if name == "TodoWrite":
            items = todos_from_input(tool_input)
            return [Emit("todos", {"items": items})] if items else []
        return [
            Emit(
                "tool_call",
                {
                    "block_id": block_id,
                    "tool": name,
                    "tool_kind": tool_kind(name),
                    "title": tool_title(name, tool_input, self.cwd),
                    "status": "running",
                    "input": tool_input,
                    "started_at": started_at,
                },
            )
        ]

    def _tool_result(self, block: dict[str, Any], row: dict[str, Any]) -> list[Emit]:
        block_id = str(block.get("tool_use_id") or "")
        if not block_id:
            return []
        record = self.tools.get(block_id, {})
        name = str(record.get("tool") or "Tool")
        if name == "TodoWrite":
            return []
        raw_input = record.get("input")
        tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        started_at = int(record.get("started_at") or now_ms())
        ended_at = now_ms()
        failed = bool(block.get("is_error"))
        fields: dict[str, Any] = {
            "block_id": block_id,
            "tool": name,
            "tool_kind": tool_kind(name),
            "title": tool_title(name, tool_input, self.cwd),
            "status": "failed" if failed else "succeeded",
            "input": tool_input,
            "output": _result_output(block, row),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": max(0, ended_at - started_at),
        }
        if not failed:
            diff = from_tool_input(name, tool_input)
            if diff:
                fields["diff"] = diff
        emits = [Emit("tool_call", fields)]
        if name == QUESTION_TOOL:
            emits.append(Emit(QUESTION_ANSWERED, {"answers": _question_answers(row)}))
        return emits


def _handed_over(attachment: dict[str, Any]) -> list[str]:
    """The ids of the messages a turn hook handed the model at a tool boundary (A51)."""
    content = attachment.get("content")
    texts = content if isinstance(content, list) else [content]
    return [
        found.group(1)
        for text in texts
        if isinstance(text, str)
        for found in _CHANNEL_TAG_ID.finditer(text)
    ]


def _first_block_index(row: dict[str, Any]) -> int:
    """Where the row's first block sits in its message, so each block has an id of its own.

    Claude Code files each block of a message as a row of its own, at position 0
    of that row, and records the block's place in the message as `apiBlockIndex`.
    Counting from the row alone gave a thought the CLI shows and the answer after
    it one id, and the answer replaced the thought. It is the index the SDK
    streams the block under, and a row read twice names the same block again. A
    row without it — a synthetic one, a whole message in one row — counts from 0.
    """
    index = row.get("apiBlockIndex")
    return index if isinstance(index, int) and index >= 0 else 0


def _question_answers(row: dict[str, Any]) -> dict[str, Any]:
    """What an `AskUserQuestion` result says was chosen, keyed by the prompt."""
    result = row.get("toolUseResult")
    if not isinstance(result, dict):
        return {}
    answers = result.get("answers")
    return answers if isinstance(answers, dict) else {}


def _result_output(block: dict[str, Any], row: dict[str, Any]) -> str:
    content = block.get("content")
    text = _text_of(content)
    if text:
        return text
    if isinstance(content, str):
        return content
    result = row.get("toolUseResult")
    if isinstance(result, dict):
        for key in ("stdout", "output", "content", "text"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return ""
