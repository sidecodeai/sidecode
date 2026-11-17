"""The part of a conversation a board update reads, and the text the model receives (A62).

A round is a message the person sent — a `user_message` whose `source` is `remote`, `terminal` or
`queue` — and every top-level event after it, in display order (`first_seq ?? seq`, A8), until the
next one. A new board reads the newest rounds; a later update reads the rounds after the last one
it read, with a few before them for context.

Everything rendered here was written by the person, by coding agents or by programs the agents ran,
so every string is untrusted: it is flattened to one line, its angle brackets are replaced so it
cannot open or close the gateway's tags, and it is cut to its limit. Tool input and output,
thinking and notices are never rendered at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .limits import CONTEXT_ROUNDS, ROUNDS, WINDOW_MAX_CHARS

Event = dict[str, Any]

#: The `user_message` sources that are the person's own words: each starts a round.
PERSON_SOURCES = frozenset({"remote", "terminal", "queue"})

CONTEXT = "context"
NEW = "new"

#: Characters each line keeps of what it quotes.
USER_CHARS = 3000
OTHER_AGENT_CHARS = 300
REPORT_CHARS = 1500
AGENT_CHARS = 300
TOOL_TITLE_CHARS = 100
REQUEST_CHARS = 200
PLAN_ITEM_CHARS = 100
QUEUED_CHARS = 500
#: A status or a category: one word the device chose from a closed list.
WORD_CHARS = 32
#: Tool lines a round keeps: the first few and the last ones, with a count between.
TOOL_HEAD = 4
TOOL_TAIL = 8
PLAN_ITEMS = 12
QUEUED_ITEMS = 5

_PLAN_MARKS = {"completed": "done", "in_progress": "doing", "pending": "todo"}
_ENDED = frozenset({"interrupted", "error"})
#: The widest "lines left out" marker, so the room measured for a cut round is never short.
_CUT_MARKER_ROOM = len("… (999999 lines left out)")


def clean(text: object) -> str:
    """One line of printable text: control and formatting characters gone, whitespace collapsed."""
    kept = "".join(
        char if char.isprintable() else " " if char.isspace() else "" for char in str(text)
    )
    return " ".join(kept.split())


#: What an angle bracket in someone else's text becomes, so it can open or close no tag.
_BRACKETS = str.maketrans({"<": "\u2039", ">": "\u203a"})


def untrusted(text: object, limit: int) -> str:
    """Text someone else wrote, made safe to place between the gateway's tags, cut to ``limit``."""
    flat = clean(text).translate(_BRACKETS)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def seq_of(event: Event) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else -1


def _position(event: Event) -> int:
    first = event.get("first_seq")
    if isinstance(first, int) and not isinstance(first, bool):
        return first
    return seq_of(event)


def display_order(events: Iterable[Event]) -> list[Event]:
    """The top-level events, in the order an app draws them."""
    top = [
        event
        for event in events
        if isinstance(event, dict) and seq_of(event) >= 0 and not event.get("parent_block_id")
    ]
    return sorted(top, key=lambda event: (_position(event), seq_of(event)))


def is_person_message(event: Event) -> bool:
    return event.get("kind") == "user_message" and event.get("source") in PERSON_SOURCES


@dataclass(frozen=True)
class Round:
    events: tuple[Event, ...]

    def is_new(self, through_seq: int) -> bool:
        """Whether anything in the round came after what the board has read."""
        return any(seq_of(event) > through_seq for event in self.events)


def split_rounds(ordered: Sequence[Event], *, reached_start: bool) -> list[Round]:
    """The rounds of a fetched range, oldest first.

    What comes before the range's first person message is the tail of an older round, kept as
    round 0 only when the fetch reached the start of the session. A range with no person message
    at all is one round.
    """
    rounds: list[Round] = []
    head: list[Event] = []
    current: list[Event] | None = None
    for event in ordered:
        if is_person_message(event):
            if current is not None:
                rounds.append(Round(tuple(current)))
            current = [event]
        elif current is None:
            head.append(event)
        else:
            current.append(event)
    if current is not None:
        rounds.append(Round(tuple(current)))
    if head and (reached_start or not rounds):
        rounds.insert(0, Round(tuple(head)))
    return rounds


def _first_new(rounds: Sequence[Round], through_seq: int) -> int | None:
    return next((at for at, item in enumerate(rounds) if item.is_new(through_seq)), None)


def _lowest_seq(events: Iterable[Event]) -> int | None:
    seqs = [seq_of(event) for event in events if seq_of(event) >= 0]
    return min(seqs) if seqs else None


def enough(events: Sequence[Event], *, through_seq: int) -> bool:
    """Whether the pages fetched so far hold what the selection needs, with older pages left.

    A new board needs `ROUNDS` person messages; a later update needs to reach back to what it read
    and `CONTEXT_ROUNDS` rounds before the first new one, unless it has more new rounds than it
    reads anyway.
    """
    ordered = display_order(events)
    if through_seq <= 0:
        return sum(1 for event in ordered if is_person_message(event)) >= ROUNDS
    rounds = split_rounds(ordered, reached_start=False)
    first = _first_new(rounds, through_seq)
    if first is None:
        return True
    if len(rounds) - first > ROUNDS:
        return True
    lowest = _lowest_seq(events)
    if lowest is None or lowest > through_seq:
        return False
    return first >= CONTEXT_ROUNDS


@dataclass(frozen=True)
class Window:
    """The rounds an update reads, each marked new or context, oldest first."""

    rounds: tuple[tuple[str, Round], ...]
    #: Older rounds of the conversation were not read.
    partial: bool

    @property
    def has_new(self) -> bool:
        return any(label == NEW for label, _ in self.rounds)


def select(events: Sequence[Event], *, reached_start: bool, through_seq: int) -> Window:
    """Choose the window from what was fetched; ``through_seq`` 0 is a new board."""
    rounds = split_rounds(display_order(events), reached_start=reached_start)
    if through_seq <= 0:
        chosen = rounds[-ROUNDS:]
        partial = not reached_start or len(rounds) > ROUNDS
        return Window(tuple((NEW, item) for item in chosen), partial)
    first = _first_new(rounds, through_seq)
    if first is None:
        return Window((), False)
    new = rounds[first:]
    lowest = _lowest_seq(events)
    reached_back = reached_start or (lowest is not None and lowest <= through_seq)
    if len(new) > ROUNDS or not reached_back:
        return Window(tuple((NEW, item) for item in new[-ROUNDS:]), True)
    context = rounds[max(0, first - CONTEXT_ROUNDS) : first]
    return Window(
        tuple((CONTEXT, item) for item in context) + tuple((NEW, item) for item in new), False
    )


@dataclass(frozen=True)
class _Line:
    kind: str
    text: str


def _word(value: object) -> str:
    return untrusted(value, WORD_CHARS) or "?"


def _user_line(event: Event) -> str | None:
    source = event.get("source")
    text = event.get("text") or ""
    if source in PERSON_SOURCES:
        attachments = event.get("attachments")
        count = len(attachments) if isinstance(attachments, list) else 0
        label = f"USER (+{count} attachments)" if count else "USER"
        return f"{label}: {untrusted(text, USER_CHARS)}"
    if source == "agent":
        return f"OTHER AGENT: {untrusted(text, OTHER_AGENT_CHARS)}"
    return None


def _question_line(event: Event) -> str:
    questions = event.get("questions")
    first = questions[0] if isinstance(questions, list) and questions else None
    prompt = first.get("prompt") if isinstance(first, dict) else ""
    return f"QUESTION {_word(event.get('status'))}: {untrusted(prompt or '', REQUEST_CHARS)}"


def _plan_line(event: Event) -> str | None:
    items = event.get("items")
    if not isinstance(items, list):
        return None
    marks = []
    for item in [entry for entry in items if isinstance(entry, dict)][:PLAN_ITEMS]:
        mark = _PLAN_MARKS.get(str(item.get("status")), "todo")
        marks.append(f"[{mark}] {untrusted(item.get('text') or '', PLAN_ITEM_CHARS)}")
    return f"AGENT PLAN: {'; '.join(marks)}" if marks else None


def _line(event: Event, *, report: bool, plan: bool) -> str | None:
    kind = event.get("kind")
    if kind == "user_message":
        return _user_line(event)
    if kind == "assistant_text":
        if event.get("done") is not True:
            return None
        limit = REPORT_CHARS if report else AGENT_CHARS
        return f"AGENT: {untrusted(event.get('text') or '', limit)}"
    if kind == "tool_call":
        title = event.get("title") or event.get("tool") or ""
        head = f"TOOL {_word(event.get('status'))} {_word(event.get('tool_kind'))}"
        return f"{head}: {untrusted(title, TOOL_TITLE_CHARS)}"
    if kind == "approval":
        title = event.get("title") or event.get("tool") or ""
        return f"APPROVAL {_word(event.get('status'))}: {untrusted(title, REQUEST_CHARS)}"
    if kind == "question":
        return _question_line(event)
    if kind == "error":
        return f"ERROR: {untrusted(event.get('message') or '', REQUEST_CHARS)}"
    if kind == "turn_completed" and event.get("stop_reason") in _ENDED:
        return f"TURN ENDED: {event['stop_reason']}"
    if kind == "todos" and plan:
        return _plan_line(event)
    return None


def _last_at(events: Sequence[Event], wanted: str, *, done: bool = False) -> int:
    for at in range(len(events) - 1, -1, -1):
        event = events[at]
        if event.get("kind") == wanted and (not done or event.get("done") is True):
            return at
    return -1


def _fold_tools(lines: list[_Line]) -> list[_Line]:
    """Keep a round's first and last tool lines, and say how many were left out between them."""
    tools = [at for at, line in enumerate(lines) if line.kind == "tool_call"]
    if len(tools) <= TOOL_HEAD + TOOL_TAIL:
        return lines
    hidden = set(tools[TOOL_HEAD:-TOOL_TAIL])
    first_hidden = tools[TOOL_HEAD]
    folded: list[_Line] = []
    for at, line in enumerate(lines):
        if at == first_hidden:
            folded.append(_Line("tool_call", f"TOOL … ({len(hidden)} more)"))
        elif at not in hidden:
            folded.append(line)
    return folded


def round_lines(item: Round) -> list[_Line]:
    """One round as the model reads it, a line per event that says something."""
    events = item.events
    report = _last_at(events, "assistant_text", done=True)
    plan = _last_at(events, "todos")
    lines: list[_Line] = []
    for at, event in enumerate(events):
        text = _line(event, report=at == report, plan=at == plan)
        if text is not None:
            kind = "user" if is_person_message(event) else str(event.get("kind"))
            lines.append(_Line(kind, text))
    return _fold_tools(lines)


def queued_lines(snapshot: Event | None) -> list[str]:
    """The session's queued messages, from the newest `queue` snapshot the gateway saw (A6)."""
    pending = snapshot.get("pending") if isinstance(snapshot, dict) else None
    if not isinstance(pending, list):
        return []
    texts = [item.get("text") or "" for item in pending if isinstance(item, dict)]
    return [f"QUEUED: {untrusted(text, QUEUED_CHARS)}" for text in texts[:QUEUED_ITEMS]]


@dataclass(frozen=True)
class Rendered:
    """The conversation as the model receives it, and what the cap left of the window."""

    text: str
    #: Rounds the cap dropped that were new: the board did not read them.
    partial: bool
    rounds_new: int
    rounds_context: int


def _compose(bodies: Sequence[tuple[str, list[_Line]]], queued: Sequence[str]) -> str:
    out: list[str] = []
    for number, (label, lines) in enumerate(bodies, start=1):
        out.append(f"--- round {number} · {label} ---")
        out.extend(line.text for line in lines)
    out.extend(queued)
    return "\n".join(out)


def _cut(lines: list[_Line], room: int) -> list[_Line]:
    """A round over the cap alone: its person's message and as many of its last lines as fit."""
    head = [line for line in lines if line.kind == "user"]
    rest = [line for line in lines if line.kind != "user"]
    used = sum(len(line.text) + 1 for line in head) + _CUT_MARKER_ROOM + 1
    tail: list[_Line] = []
    for line in reversed(rest):
        if used + len(line.text) + 1 > room:
            break
        tail.insert(0, line)
        used += len(line.text) + 1
    left_out = len(rest) - len(tail)
    if not left_out:
        return lines
    return [*head, _Line("cut", f"… ({left_out} lines left out)"), *tail]


def render(window: Window, queued: Sequence[str]) -> Rendered:
    """The window as text, at most `WINDOW_MAX_CHARS` of it.

    Over the cap, context rounds go first, oldest first, then new rounds, oldest first; the newest
    round always stays, and on its own over the cap it keeps its person's message and its end.
    """
    bodies = [(label, round_lines(item)) for label, item in window.rounds]
    partial = False
    while len(bodies) > 1 and len(_compose(bodies, queued)) > WINDOW_MAX_CHARS:
        context = next((at for at, (label, _) in enumerate(bodies) if label == CONTEXT), None)
        if context is None:
            partial = True
        del bodies[0 if context is None else context]
    if bodies and len(_compose(bodies, queued)) > WINDOW_MAX_CHARS:
        label, lines = bodies[-1]
        others = len(_compose([(label, [])], queued))
        bodies[-1] = (label, _cut(lines, WINDOW_MAX_CHARS - others))
    return Rendered(
        text=_compose(bodies, queued),
        partial=partial,
        rounds_new=sum(1 for label, _ in bodies if label == NEW),
        rounds_context=sum(1 for label, _ in bodies if label == CONTEXT),
    )
