"""The tool calls a Claude Code transcript shows, and which of them still wait for a result.

A permission prompt the person answers in the terminal leaves no word for the
device but the call's own result row. For the main thread's tools that row is
in the session's transcript, which the mirror reads as it grows. A subagent's
calls are in its own, `subagents/agent-<id>.jsonl`, which is read here from its
tail, and only while something waits on one: the prompt names the tool and
shows a preview of what it would run, never who asked, so a call is found by
what it is — its name and the text of what it runs — and by nothing else.

Finding is conservative on purpose. A call that cannot be told from another by
its text, a preview that was redacted or cut, an input with nothing long enough
to compare: each is no call at all. Mistaking a call for another would close a
card whose prompt is still waiting, and nobody would know to answer it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .previews import previewed

# How much of a transcript's end is read. A call the person is asked about is among its last
# rows: the subagent writes nothing more until the prompt is answered.
CALL_WINDOW = 256 * 1024
# A string input shorter than this is no help in telling calls apart; one up to `WHOLE_CHARS` is
# compared whole, quotes and all, and a longer one by its first `HEAD_CHARS` characters, since
# the preview cuts it.
MIN_CHARS = 4
WHOLE_CHARS = 2000
HEAD_CHARS = 200
# A prompt is about a call made this long before it, or this long after: the message that made
# the call may have been written for a while, and clocks and rows lag a little, but a call made
# well after the prompt was raised is some other call's.
LOOKBACK = 300.0
LOOKAHEAD = 5.0

_RUNS_OF_SPACE = re.compile(r"\s{2,}")


@dataclass(frozen=True, slots=True)
class Call:
    """One tool call: what it is, when it was made, and how it ended if it did.

    `needles` are pieces of the call's own text as the permission preview
    writes them, which is how a prompt is matched with it. `answers` is what an
    `AskUserQuestion` was answered, once it was.
    """

    id: str
    name: str
    needles: tuple[str, ...] = ()
    at: float | None = None
    closed: bool = False
    failed: bool = False
    answers: Mapping[str, Any] | None = None


def calls_in(rows: Iterable[Mapping[str, Any]]) -> dict[str, Call]:
    """Every call the rows make, each closed by the result row that follows it, if one does."""
    found: dict[str, Call] = {}
    for row in rows:
        kind = row.get("type")
        for block in _blocks(row):
            if kind == "assistant" and block.get("type") == "tool_use":
                call_id = block.get("id")
                if isinstance(call_id, str) and call_id:
                    found[call_id] = Call(
                        id=call_id,
                        name=str(block.get("name") or ""),
                        needles=needles_of(block.get("input")),
                        at=_stamp(row),
                    )
            elif kind == "user" and block.get("type") == "tool_result":
                opened = found.get(str(block.get("tool_use_id") or ""))
                if opened is not None:
                    found[opened.id] = replace(
                        opened,
                        closed=True,
                        failed=bool(block.get("is_error")),
                        answers=_answers(row),
                    )
    return found


def read_calls(path: str | Path, window: int = CALL_WINDOW) -> dict[str, Call]:
    """The calls in the last `window` bytes of one transcript; none when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - window))
            data = handle.read()
    except OSError:
        return {}
    lines = data.splitlines()
    if size > window:
        del lines[:1]  # the first line begins before the window, and is a fragment
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return calls_in(rows)


def open_calls(
    calls: Mapping[str, Call],
    tool: str,
    preview: str | None,
    raised: float,
    taken: Collection[str] = (),
) -> list[Call]:
    """The calls still waiting for a result that a prompt raised at `raised` may be about.

    Oldest first. A prompt for a tool with a preview is about the calls whose
    text it shows; one with none, a plan or a question, by its tool's name alone.
    """
    found = [
        call
        for call in calls.values()
        if call.name == tool
        and not call.closed
        and call.id not in taken
        and (call.at is None or raised - LOOKBACK <= call.at <= raised + LOOKAHEAD)
        and (preview is None or fits(preview, call))
    ]
    return sorted(found, key=lambda call: call.at or 0.0)


def fits(preview: str, call: Call) -> bool:
    """Whether a permission prompt's preview shows this call's text, all of it that is long enough.

    The preview writes each string input as the JSON of its value, rewritten as
    `previews.py` says, with runs of whitespace folded into one space. A call
    with no such input fits nothing.
    """
    folded = _fold(preview)
    return bool(call.needles) and all(needle in folded for needle in call.needles)


def _blocks(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def needles_of(tool_input: Any) -> tuple[str, ...]:
    """The pieces of a call's input a permission preview shows, which tell it from other calls."""
    if not isinstance(tool_input, dict):
        return ()
    found = (_needle(value) for value in tool_input.values() if isinstance(value, str))
    return tuple(needle for needle in found if needle is not None)


def _needle(value: str) -> str | None:
    if len(value) < MIN_CHARS:
        return None
    if len(value) <= WHOLE_CHARS:
        return _fold(previewed(json.dumps(value, ensure_ascii=False)))
    return _fold(previewed(json.dumps(value[:HEAD_CHARS], ensure_ascii=False)[:-1]))


def _fold(text: str) -> str:
    return _RUNS_OF_SPACE.sub(" ", text)


def _stamp(row: Mapping[str, Any]) -> float | None:
    """The row's own time, in seconds since the epoch, when it carries one."""
    stamp = row.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _answers(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """What a result row says an `AskUserQuestion` was answered, keyed by the prompt."""
    result = row.get("toolUseResult")
    answers = result.get("answers") if isinstance(result, dict) else None
    return answers if isinstance(answers, dict) else None
