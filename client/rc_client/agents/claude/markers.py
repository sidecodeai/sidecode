"""Rows Claude Code files as user turns that are not a prompt (amendment A32).

Four shapes reach a transcript with the `user` role although nobody typed
them as a message: the summary the CLI writes after compacting the context,
the marker it leaves when the person interrupts a turn, the record of a slash
command they typed, and whatever that command printed back. A mirror that
trusted the role published a page of summary as its owner's own words and
started a turn on an interruption, which then failed as a turn nobody ran.

`injected.py` decides whose words a row holds; this module decides whether a
row holds words at all, and what the turn should do about it. Every function
is pure and reads one row or its text, so the shapes can be tested straight
from a transcript.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# The `system` row Claude Code writes where the context was compacted. It sits
# before the summary and carries the token counts, which is why it, and not the
# summary, is what the apps hear about.
COMPACT_BOUNDARY = "compact_boundary"

# What the CLI writes in place of the rest of a turn the person stopped. The
# second shape rides in the row that also carries the result of the tool it
# interrupted, so the text is matched, never the row.
_INTERRUPTIONS = frozenset(
    {
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
    }
)

_COMMAND_TAG = "<command-name>"
_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)

# The CLI's own reply to a typed command. `<local-command-caveat>` shares the
# prefix but is not output: it is an instruction to the model and stays hidden
# like the other tags a command leaves behind.
_OUTPUT = "<local-command-"
_CAVEAT = "<local-command-caveat>"


def is_compaction_summary(row: Mapping[str, Any]) -> bool:
    """Whether the row is the summary the CLI wrote after compacting.

    The CLI hides it from its own view (`isVisibleInTranscriptOnly`), and the
    boundary row before it already says what happened, so a reader gains
    nothing from a page of it in the person's own bubble.
    """
    return bool(row.get("isCompactSummary"))


def is_compact_boundary(row: Mapping[str, Any]) -> bool:
    """Whether the row marks where the context was compacted.

    Asked only of `system` rows; no other row type carries a `subtype`.
    """
    return row.get("subtype") == COMPACT_BOUNDARY


def is_interruption(text: str) -> bool:
    """Whether the text is the marker left where the person stopped a turn.

    It ends the turn rather than starting one, and it is never a block: the
    apps already show an interrupted turn as interrupted.
    """
    return text.strip() in _INTERRUPTIONS


def is_command_output(text: str) -> bool:
    """Whether the text is what the CLI printed in reply to a typed command.

    The reply is the end of what the command did, so it closes a turn the
    command's own row left open, and it carries terminal escapes no app can
    show, so it is never a block.
    """
    body = text.lstrip()
    return body.startswith(_OUTPUT) and not body.startswith(_CAVEAT)


def typed_command(text: str) -> str | None:
    """The slash command the person typed, as they typed it, or `None`.

    The CLI records a command as `<command-name>` with its argument in a tag of
    its own; `<command-message>` only repeats the name without its slash. What
    comes back is what the terminal shows — `/model haiku`, or `/compact` when
    the command took no argument.
    """
    body = text.lstrip()
    if not body.startswith(_COMMAND_TAG):
        return None
    match = _NAME.search(body)
    name = match.group(1).strip() if match else ""
    if not name:
        return None
    found = _ARGS.search(body)
    args = found.group(1).strip() if found else ""
    return f"{name} {args}" if args else name
