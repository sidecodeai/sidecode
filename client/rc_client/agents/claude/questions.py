"""`AskUserQuestion` in both directions: the tool's shape and the protocol's.

Two paths reach this module. A session the device drives sees the question
through the SDK's permission callback; a session a terminal owns sees it through
the `PermissionRequest` hook Claude Code runs beside its own dialog (amendment
A20). Both raise the same `question` block, and both have to hand Claude the
answer in the shape its own dialog would have produced, so the mapping lives
here rather than in either caller.
"""

from __future__ import annotations

from typing import Any

QUESTION_TOOL = "AskUserQuestion"

MAX_QUESTIONS = 4
MAX_OPTIONS = 8
MAX_PROMPT = 2000
MAX_DESCRIPTION = 400


def normalise_questions(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn an `AskUserQuestion` input into protocol `question.questions`."""
    raw = tool_input.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("AskUserQuestion requires at least one question")
    questions: list[dict[str, Any]] = []
    for index, entry in enumerate(raw[:MAX_QUESTIONS]):
        if not isinstance(entry, dict):
            continue
        prompt = str(entry.get("question") or entry.get("prompt") or "").strip()
        if not prompt:
            continue
        questions.append(
            {
                "id": f"q{index}",
                "prompt": prompt[:MAX_PROMPT],
                "options": _options(entry.get("options")),
                "multi": bool(entry.get("multiSelect") or entry.get("multi")),
                "allow_text": True,
            }
        )
    if not questions:
        raise ValueError("AskUserQuestion had no usable questions")
    return questions


def _options(raw: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for position, option in enumerate(raw or []):
        if position >= MAX_OPTIONS:
            break
        if isinstance(option, str):
            label, description = option, ""
        elif isinstance(option, dict):
            label = str(option.get("label") or option.get("id") or "")
            description = str(option.get("description") or "")
        else:
            continue
        if not label:
            continue
        # Ids are positional: two options sharing a label would otherwise
        # collide and the answer would be routed to the wrong one.
        item = {"id": f"o{position}", "label": label}
        if description:
            item["description"] = description[:MAX_DESCRIPTION]
        options.append(item)
    return options


def answers_by_prompt(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, Any]:
    """Turn positional option ids back into the labels Claude expects."""
    resolved: dict[str, Any] = {}
    for question in questions:
        value = answers.get(question["id"])
        if value is None:
            continue
        labels = {option["id"]: option["label"] for option in question["options"]}
        if isinstance(value, list):
            resolved[question["prompt"]] = [labels.get(str(item), str(item)) for item in value]
        else:
            resolved[question["prompt"]] = labels.get(str(value), value)
    return resolved


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
        ids = _option_ids(question, value)
        if ids is not None:
            resolved[question["id"]] = ids
        elif isinstance(value, str) and value.strip():
            resolved[question["id"]] = value
    return resolved


def _option_ids(question: dict[str, Any], value: Any) -> list[str] | None:
    """The option ids `value` names, or None when it names anything else.

    Every part has to be a label for the answer to be a choice: half a match is
    free text the person typed, not a selection the ids can carry.
    """
    labels = {str(option["label"]): str(option["id"]) for option in question["options"]}
    if isinstance(value, list):
        chosen = [labels[str(item)] for item in value if str(item) in labels]
        return chosen if chosen and len(chosen) == len(value) else None
    if not isinstance(value, str):
        return None
    if value in labels:
        return [labels[value]]
    parts = [part.strip() for part in value.split(",")]
    chosen = [labels[part] for part in parts if part in labels]
    return chosen if len(parts) > 1 and len(chosen) == len(parts) else None
