"""Grok Build's `ask_user_question`, mapped both ways (A52).

Every request and tool result here was recorded from Grok 1.0.62
(`fixtures/grok/question-endings.json`): a private child answered from the
device, a free-text and a multi-select answer, a refusal, Grok's own deadline,
and the failure an error reply causes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.grok.questions import (
    ANSWERED,
    CONTINUE,
    Asked,
    accepted,
    answered_elsewhere,
    asked,
    ended,
)

ENDINGS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "grok" / "question-endings.json").read_text(
        encoding="utf-8"
    )
)


def recorded(name: str) -> Asked:
    return asked(ENDINGS[name]["request"])


def test_the_request_becomes_the_protocol_s_block() -> None:
    [color] = recorded("answered").blocks
    assert color == {
        "id": "q0",
        "prompt": "Which color should I use?",
        "options": [
            {"id": "o0", "label": "Red", "description": "warm"},
            {"id": "o1", "label": "Blue", "description": "cool"},
        ],
        "multi": False,
        "allow_text": True,
    }
    [toppings] = recorded("multi_select").blocks
    assert toppings["multi"] is True


def test_a_request_with_no_question_is_refused_as_unusable() -> None:
    with pytest.raises(ValueError):
        asked({"sessionId": "s", "toolCallId": "c", "questions": [], "mode": "default"})


def test_an_app_s_choice_goes_back_in_the_shape_grok_takes() -> None:
    """Apps send every choice as a list of option ids; a single choice goes back alone."""
    color = recorded("answered")
    assert accepted(color, {"q0": ["o1"]}) == {
        "outcome": "accepted",
        "answers": {"Which color should I use?": "Blue"},
    }
    assert accepted(color, {"q0": "o1"})["answers"] == {"Which color should I use?": "Blue"}
    assert accepted(color, {"q0": "Green"})["answers"] == {"Which color should I use?": "Green"}
    toppings = recorded("multi_select")
    assert accepted(toppings, {"q0": ["o0", "o1"]})["answers"] == {
        "Which toppings?": ["Cheese", "Ham"]
    }
    assert accepted(toppings, {"q0": ["o2"]})["answers"] == {"Which toppings?": ["Olives"]}


def test_the_reply_names_each_question_in_grok_s_own_words() -> None:
    """The block's prompt is trimmed and capped for display; the reply's key is not."""
    long = "Which of these? " + "x" * 2100
    question = asked(
        {
            "questions": [
                {"question": "  Which color?  ", "options": [{"label": "Red", "description": "r"}]},
                {"question": long, "options": [{"label": "This", "description": "t"}]},
            ]
        }
    )
    first, second = question.blocks
    assert first["prompt"] == "Which color?"
    assert len(second["prompt"]) == 2000
    assert accepted(question, {"q0": ["o0"], "q1": ["o0"]})["answers"] == {
        "  Which color?  ": "Red",
        long: "This",
    }


def test_a_question_past_the_fourth_is_not_shown_or_answered() -> None:
    entries = [
        {"question": f"Question {n}?", "options": [{"label": "Yes", "description": "y"}]}
        for n in range(1, 6)
    ]
    question = asked({"questions": entries})
    assert [block["prompt"] for block in question.blocks] == [
        "Question 1?",
        "Question 2?",
        "Question 3?",
        "Question 4?",
    ]
    answers = {block["id"]: ["o0"] for block in question.blocks}
    assert accepted(question, answers)["answers"] == {
        "Question 1?": "Yes",
        "Question 2?": "Yes",
        "Question 3?": "Yes",
        "Question 4?": "Yes",
    }


@pytest.mark.parametrize(
    ("name", "chosen"),
    [
        ("answered", {"q0": ["o1"]}),
        ("multi_select", {"q0": ["o0", "o1"]}),
        ("free_text", {}),
        ("declined", None),
        ("timed_out", None),
        ("failed", None),
    ],
)
def test_the_tool_s_result_says_what_somebody_else_chose(
    name: str, chosen: dict[str, list[str]] | None
) -> None:
    result = ENDINGS[name]["result"]
    assert ended(result) is True
    assert answered_elsewhere(recorded(name), result) == chosen


def summary(*pairs: str) -> dict[str, Any]:
    """A result in the tool's own words, for answers no live run produced."""
    message = f"{ANSWERED}{', '.join(pairs)}{CONTINUE}"
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "status": "completed",
        "rawOutput": {"type": "AskUserQuestion", "UserAnswered": {"message": message}},
    }


def test_each_question_takes_its_own_answer() -> None:
    question = asked(
        {
            "questions": [
                {"question": "Which color?", "options": [{"label": "Red"}, {"label": "Blue"}]},
                {"question": "Which size?", "options": [{"label": "S"}, {"label": "L"}]},
                {"question": " Anything else? ", "options": [{"label": "No"}]},
            ]
        }
    )
    result = summary('"Which color?"="Blue"', '"Which size?"="XL"', '" Anything else? "="No"')
    assert answered_elsewhere(question, result) == {"q0": ["o1"], "q2": ["o0"]}


def test_notes_after_an_answer_are_not_part_of_it() -> None:
    question = asked({"questions": [{"question": "Which color?", "options": [{"label": "Red"}]}]})
    noted = summary('"Which color?"="Red" selected preview: #ff0000 user notes: brighter')
    assert answered_elsewhere(question, noted) == {"q0": ["o0"]}


def test_a_result_still_running_is_not_an_ending() -> None:
    assert ended({"sessionUpdate": "tool_call_update", "status": None}) is False
