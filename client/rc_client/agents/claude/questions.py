"""`AskUserQuestion` in both directions: the tool's shape and the protocol's.

Two paths reach this module. A session the device drives sees the question
through the SDK's permission callback; a session a terminal owns sees it through
the `PermissionRequest` hook Claude Code runs beside its own dialog (amendment
A20). Both raise the same `question` block, and both have to hand Claude the
answer in the shape its own dialog would have produced, so the mapping lives
here rather than in either caller. The block itself is shared with Grok Build
(`agents/questions.py`); what is here is Claude's own side of it.
"""

from __future__ import annotations

from typing import Any

from ..questions import answers_by_prompt, option_ids

QUESTION_TOOL = "AskUserQuestion"


def answers_for_tool(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, str]:
    """The answers as the CLI's own dialog records them: one string per question.

    A multi-select is the chosen labels joined with commas, which is what the
    hook has to hand back for the tool to read the answer as its own.
    """
    resolved: dict[str, str] = {}
    for prompt, value in answers_by_prompt(questions, answers).items():
        resolved[str(prompt)] = (
            ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        )
    return resolved


def answers_from_tool(questions: list[dict[str, Any]], raw: Any) -> dict[str, Any]:
    """Read a terminal's own answers back into protocol `question.answers`.

    The tool result records what the person chose under the question's prompt
    and by the option's label. An answer that names no option is the tool's
    "Type something" path, which is free text and passes through as it is.
    """
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, Any] = {}
    for question in questions:
        value = raw.get(question["prompt"])
        ids = option_ids(question, value)
        if ids is not None:
            resolved[question["id"]] = ids
        elif isinstance(value, str) and value.strip():
            resolved[question["id"]] = value
    return resolved
