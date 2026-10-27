"""Which call of a subagent a permission prompt, a plan or a question is waiting on (A20, A51).

A prompt a subagent raised is relayed to the device with a tool's name and a
preview of what it would run, and nothing to say who asked, or whether it is a
subagent's at all: the main thread's prompts arrive the same way. When the
person answers it in the terminal, only the call's own result row, in the
subagent's transcript, says so, so the prompt has to be matched with the call
first. `agents/claude/calls.py` finds the calls that fit; what is decided here
is when a fit is good enough to act on, and the answer is: only when it cannot
be another call's. A prompt is paired with a call when that call is the only
open one it fits, no other prompt fits that call, and none of the main thread's
open calls fits the prompt. Anything else — two calls that look alike, two
prompts that do, a call that may be the main thread's, a call whose row is not
written yet — is no pairing, and the card stays until something certain closes
it: a card kept is only a card, and a card closed wrongly is a prompt nobody
sees. The same goes for the end of a paired call, which answers its prompt only
while no other open call could be the one the prompt waited on.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, Protocol

from ..agents.claude.calls import Call, fits, needles_of, open_calls


class Asking(Protocol):
    """A prompt the CLI is waiting on the person for, and the call it was found to wait on."""

    raised: float
    call: str | None


class ApprovalAsking(Asking, Protocol):
    """A permission prompt or a plan: a tool's name, and for a tool the preview of its input."""

    tool: str
    plan: dict[str, Any] | None
    input: dict[str, Any]


def bind_approvals(
    pending: Sequence[ApprovalAsking],
    calls: Mapping[str, Call],
    rivals: Mapping[str, Call],
    taken: Collection[str],
) -> None:
    """Pair each approval that waits on no call yet with the one call it can only be about.

    `calls` are the subagents' and `rivals` the main thread's.
    """
    fitting = {id(approval): _fitting(approval, calls, taken) for approval in pending}
    for approval in pending:
        found = fitting[id(approval)]
        if len(found) != 1 or _fitting(approval, rivals, ()):
            continue
        if sum(1 for other in fitting.values() if found[0] in other) == 1:
            approval.call = found[0]


def bind_question(
    question: Asking,
    calls: Mapping[str, Call],
    rivals: Mapping[str, Call],
    taken: Collection[str],
    tool: str,
) -> None:
    """Pair a question with the one open call of `tool` that can be it.

    The main thread asks questions too, so one it has an open call of the same
    tool for may be its own, and is paired with nothing.
    """
    found = open_calls(calls, tool, None, question.raised, taken)
    if len(found) == 1 and not open_calls(rivals, tool, None, question.raised):
        question.call = found[0].id


def about_input(approval: ApprovalAsking, tool_input: Any) -> bool:
    """Whether an approval is about a call of its tool with this input, as far as its preview shows.

    A plan has no preview to compare, and only the main thread asks for one.
    """
    if approval.plan is not None:
        return True
    call = Call(id="", name=approval.tool, needles=needles_of(tool_input))
    return fits(str(approval.input.get("input_preview") or ""), call)


def answered_by_result[Approval: ApprovalAsking](
    approvals: Iterable[Approval], tool: str, tool_input: Any, subagents: bool
) -> Approval | None:
    """The one prompt that a result of the main thread's `tool`, run with this input, answers.

    The candidates are the prompts of that tool that no subagent's call is
    paired with, and those that show this input are the ones. With subagents
    about, and an input that has text to show, a prompt that shows none of it is
    a subagent's, so none is answered; with no subagent, or nothing in the input
    to compare, the prompts of the tool are all there is. A prompt is answered
    only when it is the one left: with several, which was answered cannot be
    told, and a card kept is only a card.
    """
    asked = [approval for approval in approvals if approval.tool == tool and approval.call is None]
    shown = [approval for approval in asked if about_input(approval, tool_input)]
    if shown:
        asked = shown
    elif subagents and needles_of(tool_input):
        return None
    return asked[0] if len(asked) == 1 else None


def contested_approval(
    approval: ApprovalAsking, calls: Mapping[str, Call], rivals: Mapping[str, Call]
) -> bool:
    """Whether some open call besides the one it was paired with could be the one it waits on.

    A subagent's, or the main thread's: the pairing was a guess, and is let go.
    """
    own = {approval.call} if approval.call else ()
    return bool(_fitting(approval, calls, own) or _fitting(approval, rivals, ()))


def contested_question(
    question: Asking, calls: Mapping[str, Call], rivals: Mapping[str, Call], tool: str
) -> bool:
    own = {question.call} if question.call else ()
    found = open_calls(calls, tool, None, question.raised, own)
    return bool(found or open_calls(rivals, tool, None, question.raised))


def _fitting(
    approval: ApprovalAsking, calls: Mapping[str, Call], taken: Collection[str]
) -> list[str]:
    """The ids of the open calls an approval may be about.

    A plan is about one by its tool alone, any other prompt by its preview.
    """
    preview = None if approval.plan is not None else str(approval.input.get("input_preview") or "")
    if preview == "":
        return []
    found = open_calls(calls, approval.tool, preview, approval.raised, taken)
    return [call.id for call in found]
