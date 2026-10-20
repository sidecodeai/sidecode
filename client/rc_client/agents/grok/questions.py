"""Grok Build's `ask_user_question`, in both directions (A52).

The tool does not wait on a terminal of its own: it sends its client the ACP
extension request `_x.ai/ask_user_question {sessionId, toolCallId, questions,
mode}` and waits for the reply. `{"outcome": "accepted", "answers": {question:
label}}` hands it an answer, `{"outcome": "cancelled"}` says the person declined,
and a JSON-RPC error is final as well: the tool fails and the model carries on
without asking again. On the leader every client of the session is asked at
once and the first reply wins, so the tool's own result is the one record of
what was chosen there.

An app sees at most four questions of eight options (`agents/questions.py`).
A question past the fourth is not shown and its answer is left out of the
reply; Grok reports only the questions it was given answers to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..questions import chosen_labels, normalise_questions, option_ids, question_id
from .translate import text_of

DECLINED: dict[str, Any] = {"outcome": "cancelled"}
# How the tool's result begins and ends when the person chose something.
ANSWERED = "User has answered your questions: "
CONTINUE = ". You can now continue with the user's answers in mind."
# What may follow an answer's closing quote: the next question, or the notes
# the terminal lets a person add to a choice.
_AFTER_ANSWER = (', "', " selected preview:", " user notes:")
_ENDED = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class Asked:
    """One request's questions: the blocks an app is shown, and Grok's own words for each.

    The block's prompt is trimmed and capped for display; the reply and the
    tool's summary both name a question exactly as Grok asked it.
    """

    blocks: list[dict[str, Any]]
    wording: dict[str, str]


def asked(params: dict[str, Any]) -> Asked:
    """What one `_x.ai/ask_user_question` asks; `ValueError` when it asks nothing usable.

    Grok's questions take the shape Claude's do. An option's `preview` has no
    place in the block and is left out.
    """
    blocks = normalise_questions(params)
    entries = params.get("questions") or []
    said = {
        question_id(index): str(entry.get("question") or entry.get("prompt") or "")
        for index, entry in enumerate(entries)
        if isinstance(entry, dict)
    }
    return Asked(blocks, {block["id"]: said[block["id"]] for block in blocks})


def accepted(question: Asked, answers: dict[str, Any]) -> dict[str, Any]:
    """The reply that hands Grok an app's answer, keyed by each question in Grok's words.

    An app sends a choice as a list of option ids whatever the question takes;
    a question that takes one choice gets its label back alone, the way Grok's
    own card answers it.
    """
    chosen: dict[str, Any] = {}
    for block in question.blocks:
        value = answers.get(block["id"])
        if value is None:
            continue
        label = chosen_labels(block, value)
        if isinstance(label, list) and len(label) == 1 and not block["multi"]:
            label = label[0]
        chosen[question.wording[block["id"]]] = label
    return {"outcome": "accepted", "answers": chosen}


def ended(update: dict[str, Any]) -> bool:
    """Whether a `tool_call_update` is the tool's last word."""
    return update.get("status") in _ENDED


def answered_elsewhere(question: Asked, update: dict[str, Any]) -> dict[str, list[str]] | None:
    """What somebody else chose, read from the tool's result; `None` when nobody did.

    Only an exact match counts: the question in Grok's words and option labels
    as they were offered. Free text, or anything the result words differently,
    is left out rather than guessed at, so the map may be empty.
    """
    text = _result_text(update)
    if not text.startswith(ANSWERED):
        return None
    body = text[len(ANSWERED) :]
    if body.endswith(CONTINUE):
        body = body[: -len(CONTINUE)]
    found: dict[str, list[str]] = {}
    for block in question.blocks:
        value = _answer_to(body, question.wording[block["id"]])
        ids = option_ids(block, value) if value is not None else None
        if ids:
            found[str(block["id"])] = ids
    return found


def _result_text(update: dict[str, Any]) -> str:
    raw = update.get("rawOutput")
    answered = raw.get("UserAnswered") if isinstance(raw, dict) else None
    message = answered.get("message") if isinstance(answered, dict) else None
    if isinstance(message, str):
        return message
    return text_of(update.get("content"))


def _answer_to(body: str, prompt: str) -> str | None:
    """The answer the summary gives one question: `"<question>"="<answer>"`."""
    opening = f'"{prompt}"="'
    start = body.find(opening)
    if start < 0:
        return None
    rest = body[start + len(opening) :]
    for index, char in enumerate(rest):
        if char != '"':
            continue
        tail = rest[index + 1 :]
        if not tail or tail.startswith(_AFTER_ANSWER):
            return rest[:index]
    return None
