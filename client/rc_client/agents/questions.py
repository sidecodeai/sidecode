"""A question an agent asks the person, in the protocol's shape and back.

Claude Code's `AskUserQuestion` and Grok Build's `ask_user_question` take the
same input — a list of questions, each with its options and whether several may
be picked — and both want the answer back as the chosen labels keyed by the
question as it was asked. The `question` block in between is the protocol's own:
positional ids, which this module maps in both directions.
"""

from __future__ import annotations

from typing import Any

MAX_QUESTIONS = 4
MAX_OPTIONS = 8
MAX_PROMPT = 2000
MAX_DESCRIPTION = 400


def question_id(index: int) -> str:
    """The block's id for the question at `index` of the agent's own list."""
    return f"q{index}"


def normalise_questions(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn an agent's question input into protocol `question.questions`."""
    raw = tool_input.get("questions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("a question needs at least one entry")
    questions: list[dict[str, Any]] = []
    for index, entry in enumerate(raw[:MAX_QUESTIONS]):
        if not isinstance(entry, dict):
            continue
        prompt = str(entry.get("question") or entry.get("prompt") or "").strip()
        if not prompt:
            continue
        questions.append(
            {
                "id": question_id(index),
                "prompt": prompt[:MAX_PROMPT],
                "options": _options(entry.get("options")),
                "multi": bool(entry.get("multiSelect") or entry.get("multi")),
                "allow_text": True,
            }
        )
    if not questions:
        raise ValueError("the question had no usable entries")
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
    """Turn positional option ids back into the labels an agent expects."""
    resolved: dict[str, Any] = {}
    for question in questions:
        value = answers.get(question["id"])
        if value is not None:
            resolved[question["prompt"]] = chosen_labels(question, value)
    return resolved


def chosen_labels(question: dict[str, Any], value: Any) -> Any:
    """One answer with its option ids turned back into labels; free text passes through."""
    labels = {option["id"]: option["label"] for option in question["options"]}
    if isinstance(value, list):
        return [labels.get(str(item), str(item)) for item in value]
    return labels.get(str(value), value)


def option_ids(question: dict[str, Any], value: Any) -> list[str] | None:
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
