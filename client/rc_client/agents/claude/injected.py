"""Which user rows are the person's words and which another agent's (A30).

Claude Code files three different things as `user` turns. One is a prompt
somebody typed. The other two are words nobody typed: the message one Claude
session sends another, which arrives wrapped in a `<teammate-message>` envelope
around a JSON report, and the notification a background task or a subagent
leaves when it stops, which arrives as `<task-notification>`. A mirror that
trusts the role shows both as terminal input, so a phone reads a wall of a
teammate's JSON as its owner's own words.

This module is the whole decision, and it is pure: it reads a row and the text
of its message and says what to publish. `<system-reminder>` blocks are the
CLI's own instructions to the model and are never anybody's words, so they come
off first, wherever they appear and however many there are.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_TEAMMATE = re.compile(r"<teammate-message\b[^>]*>(.*?)</teammate-message>", re.DOTALL)
_TASK = re.compile(r"<task-notification\b[^>]*>(.*?)</task-notification>", re.DOTALL)

# What a teammate's JSON report calls the thing it wants to say, best first.
_SAID = ("result", "summary", "message", "text")
# What a task notification is worth reading: what finished, and what it found.
_REPORTED = ("summary", "result")

# `origin.kind` values that are not another agent: "human" is the person at the
# keyboard, and "channel" is this device's own injection, which the transcript
# reader correlates by `message_id` and keeps out of the timeline entirely.
_NOT_AGENT = frozenset({"human", "channel"})


@dataclass(slots=True, frozen=True)
class Injected:
    """What one user row should publish: its text, and whose words they are."""

    text: str
    by_agent: bool


def strip_reminders(text: str) -> str:
    """Drop every `<system-reminder>` block; what is left is somebody's words."""
    return _REMINDER.sub("", text).strip()


def classify(row: Mapping[str, Any], text: str) -> Injected | None:
    """Reduce one user row to what a reader wants, or to nothing at all.

    `None` means the row holds no message: a turn that carried only a
    `<system-reminder>`, or a notification with nothing to report.
    """
    said = strip_reminders(text)
    reduced = _teammate(said)
    if reduced is None:
        reduced = _task(said)
    by_agent = reduced is not None or from_agent(row)
    body = said if reduced is None else reduced
    return Injected(text=body, by_agent=by_agent) if body else None


def from_agent(row: Mapping[str, Any]) -> bool:
    """Whether the CLI itself says this turn was not typed by the person.

    Older versions attribute nothing, which is why the envelopes above are read
    as well; newer ones carry an `origin` and a `promptSource` on the row, and
    the SDK's own advice for a `kind` it does not know is "not human".
    """
    origin = row.get("origin")
    kind = origin.get("kind") if isinstance(origin, Mapping) else None
    if isinstance(kind, str) and kind in _NOT_AGENT:
        return False
    if row.get("promptSource") == "system":
        return True
    return isinstance(kind, str) and bool(kind)


def _teammate(text: str) -> str | None:
    """`"<from>: <result>"` out of a teammate envelope, or `None` if it is not one."""
    match = _TEAMMATE.search(text)
    if match is None:
        return None
    body = match.group(1).strip()
    report = _report(body)
    if report is None:
        return body
    who = _string(report.get("from"))
    for key in _SAID:
        said = _string(report.get(key))
        if said:
            return f"{who}: {said}" if who else said
    return f"{who}: {body}" if who else body


def _report(body: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _task(text: str) -> str | None:
    """A task notification's summary and result, or `None` if it is not one.

    Everything else the envelope carries — the task id, the tool use, the file
    the output went to — names plumbing the reader has no use for.
    """
    match = _TASK.search(text)
    if match is None:
        return None
    body = match.group(1)
    lines = [_tag(body, name) for name in _REPORTED]
    return "\n".join(line for line in lines if line)


def _tag(body: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", body, re.DOTALL)
    return match.group(1).strip() if match else ""


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
