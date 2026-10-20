"""The question block Claude Code and Grok Build share, in both directions."""

from __future__ import annotations

from rc_client.agents.questions import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    answers_by_prompt,
    normalise_questions,
    option_ids,
)

# `_x.ai/ask_user_question` as Grok 1.0.62 asked it live: the shape Claude's
# `AskUserQuestion` input has, `multiSelect` null for a single choice.
GROK_INPUT = {
    "questions": [
        {
            "question": "Which color should I use?",
            "options": [
                {"label": "Red", "description": "warm"},
                {"label": "Blue", "description": "cool", "preview": "#0000ff"},
            ],
            "multiSelect": None,
        },
        {
            "question": "Which toppings?",
            "options": [{"label": "Cheese, mild"}, {"label": "Ham"}, {"label": "Olives"}],
            "multiSelect": True,
        },
    ],
    "mode": "default",
}


def test_a_grok_question_becomes_the_protocol_s_block() -> None:
    color, toppings = normalise_questions(GROK_INPUT)
    assert color == {
        "id": "q0",
        "prompt": "Which color should I use?",
        "options": [
            {"id": "o0", "label": "Red", "description": "warm"},
            {"id": "o1", "label": "Blue", "description": "cool"},
        ],
        "multi": False,
        "allow_text": True,
    }, "a preview has no place in the block"
    assert toppings["multi"] is True
    assert [option["id"] for option in toppings["options"]] == ["o0", "o1", "o2"]


def test_the_block_keeps_to_four_questions_of_eight_options() -> None:
    many = {
        "questions": [
            {"question": f"Question {n}?", "options": [f"Option {k}" for k in range(12)]}
            for n in range(6)
        ]
    }
    questions = normalise_questions(many)
    assert len(questions) == MAX_QUESTIONS
    assert all(len(question["options"]) == MAX_OPTIONS for question in questions)


def test_answers_go_back_as_labels_keyed_by_the_question() -> None:
    questions = normalise_questions(GROK_INPUT)
    assert answers_by_prompt(questions, {"q0": "o1", "q1": ["o0", "o2"]}) == {
        "Which color should I use?": "Blue",
        "Which toppings?": ["Cheese, mild", "Olives"],
    }
    assert answers_by_prompt(questions, {"q0": "Green"}) == {
        "Which color should I use?": "Green"
    }, "free text passes through as typed"


def test_only_labels_name_options() -> None:
    color, toppings = normalise_questions(GROK_INPUT)
    assert option_ids(color, "Blue") == ["o1"]
    assert option_ids(color, "Green") is None
    assert option_ids(toppings, "Cheese, mild") == ["o0"], "a label with a comma is one label"
    assert option_ids(toppings, "Ham, Olives") == ["o1", "o2"]
    assert option_ids(toppings, "Ham, Anchovies") is None, "half a match is free text"
    assert option_ids(toppings, ["Ham", "Olives"]) == ["o1", "o2"]
    assert option_ids(toppings, None) is None
