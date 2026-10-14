"""Codex `item/tool/requestUserInput` prompts, in both directions.

Pure translation, shared by the private app-server adapter and the shared
daemon client: the daemon asks its questions the same way whichever connection
carries them.
"""

from __future__ import annotations

from typing import Any

MAX_QUESTIONS = 4


def question_blocks(params: dict[str, Any]) -> list[dict[str, Any]]:
    """The protocol `question.questions` array for one Codex request."""
    questions: list[dict[str, Any]] = []
    for index, raw in enumerate((params.get("questions") or [])[:MAX_QUESTIONS]):
        if not isinstance(raw, dict):
            continue
        options = [
            {
                "id": f"o{position}",
                "label": str(option.get("label") or ""),
                "description": str(option.get("description") or ""),
            }
            for position, option in enumerate(raw.get("options") or [])
            if isinstance(option, dict) and option.get("label")
        ]
        questions.append(
            {
                "id": str(raw.get("id") or index),
                "prompt": str(raw.get("question") or raw.get("header") or ""),
                "options": options,
                "multi": False,
                "allow_text": bool(raw.get("isOther", True)),
                "secret": bool(raw.get("isSecret")),
            }
        )
    return questions


def answers_payload(
    questions: list[dict[str, Any]], answers: dict[str, Any]
) -> dict[str, dict[str, list[str]]]:
    """Codex wants the option *labels* back, not the ids the apps chose."""
    labels = {
        str(question["id"]): {
            str(option["id"]): str(option["label"]) for option in question["options"]
        }
        for question in questions
    }
    payload: dict[str, dict[str, list[str]]] = {}
    for key, value in answers.items():
        chosen = value if isinstance(value, list) else [value]
        by_id = labels.get(str(key), {})
        payload[str(key)] = {"answers": [str(by_id.get(str(item), item)) for item in chosen]}
    return payload
