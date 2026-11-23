"""`agy`'s questions, as one `question` block and back (A64, 5.8).

`agy` asks with an `elicitation/create` form, which it sends only to a client that declares form
elicitation. The form carries each question as two fields: `qN`, the choices — a string with `oneOf`
for one choice, an `array` whose `items` carry `anyOf` for several, each choice a `const` and its
`title` — and `qN_other`, free words. The block has one question per `qN`, in the form's order and
at most four, under the field's own name, with the choices' `const`s as option ids. The answer goes
back as `accept`: the chosen `const` for one choice, a list of them for several, or the words in
`qN_other`; a question nobody answered is left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..questions import MAX_OPTIONS, MAX_PROMPT, MAX_QUESTIONS

_QUESTION = re.compile(r"^q\d+$")
OTHER = "_other"


@dataclass(frozen=True, slots=True)
class Form:
    """One form's questions as the block shows them, and where each one's free words go."""

    questions: list[dict[str, Any]] = field(default_factory=list)
    others: dict[str, str] = field(default_factory=dict)

    def block(self) -> list[dict[str, Any]]:
        return [dict(question) for question in self.questions]


def read(params: dict[str, Any]) -> Form:
    """The questions one `elicitation/create` asks; `ValueError` when none can be shown."""
    schema = params.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        raise ValueError("a form needs properties")
    questions: list[dict[str, Any]] = []
    others: dict[str, str] = {}
    for name, spec in properties.items():
        if len(questions) >= MAX_QUESTIONS:
            break
        if not _QUESTION.match(str(name)) or not isinstance(spec, dict):
            continue
        question = _question(str(name), spec, properties)
        if question is None:
            continue
        if isinstance(properties.get(f"{name}{OTHER}"), dict):
            others[str(name)] = f"{name}{OTHER}"
        questions.append(question)
    if not questions:
        raise ValueError("the form had no question the device can show")
    return Form(questions, others)


def _question(name: str, spec: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any] | None:
    prompt = str(spec.get("title") or spec.get("description") or "").strip()
    if not prompt:
        return None
    multi = spec.get("type") == "array"
    items = spec.get("items") if multi else None
    choices = items.get("anyOf") if isinstance(items, dict) else spec.get("oneOf")
    options = _options(choices)
    free = isinstance(properties.get(f"{name}{OTHER}"), dict)
    if not options and spec.get("type") != "string":
        return None
    return {
        "id": name,
        "prompt": prompt[:MAX_PROMPT],
        "options": options,
        "multi": multi,
        # Words go to the question's `_other` field, or to the field itself when it has no choices.
        "allow_text": free or not options,
    }


def _options(choices: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for choice in choices if isinstance(choices, list) else []:
        if len(options) >= MAX_OPTIONS:
            break
        if not isinstance(choice, dict):
            continue
        value = choice.get("const")
        if value is None or isinstance(value, bool | dict | list):
            continue
        label = str(choice.get("title") or value)
        options.append({"id": str(value), "label": label})
    return options


def accepted(form: Form, answers: dict[str, Any]) -> dict[str, Any]:
    """The reply that hands `agy` an app's answers (5.8: a list of option ids, or free words)."""
    content: dict[str, Any] = {}
    for question in form.questions:
        value = answers.get(question["id"])
        if isinstance(value, list):
            _choose(content, question, value)
        elif isinstance(value, str) and value.strip():
            _words(content, form, question, value)
    return {"action": "accept", "content": content}


def _choose(content: dict[str, Any], question: dict[str, Any], value: list[Any]) -> None:
    offered = [option["id"] for option in question["options"]]
    chosen = [item for item in (str(entry) for entry in value) if item in offered]
    if not chosen:
        return
    content[question["id"]] = chosen if question["multi"] else chosen[0]


def _words(content: dict[str, Any], form: Form, question: dict[str, Any], value: str) -> None:
    other = form.others.get(question["id"])
    if other is not None:
        content[other] = value
    elif not question["options"]:
        content[question["id"]] = value
