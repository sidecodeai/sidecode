"""`agy`'s forms as one question block, and the replay a device publishes only where new (A64)."""

from __future__ import annotations

from typing import Any

import pytest

from rc_client.agents.antigravity import forms, replay
from rc_client.agents.antigravity.replay import AGENT, USER, Ledger, Replay


def form(properties: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "form", "requestedSchema": {"type": "object", "properties": properties}}


def choice(const: str, title: str) -> dict[str, str]:
    return {"const": const, "title": title}


def test_qn_is_a_question_and_qn_other_its_words() -> None:
    read = forms.read(
        form(
            {
                "q1": {
                    "type": "string",
                    "title": "Pick one",
                    "oneOf": [choice("1", "a"), choice("2", "b")],
                },
                "q1_other": {"type": "string", "title": "Other"},
                "q2": {
                    "type": "array",
                    "title": "Pick some",
                    "items": {"anyOf": [choice("1", "x")]},
                },
            }
        )
    )
    assert read.questions == [
        {
            "id": "q1",
            "prompt": "Pick one",
            "options": [{"id": "1", "label": "a"}, {"id": "2", "label": "b"}],
            "multi": False,
            "allow_text": True,
        },
        {
            "id": "q2",
            "prompt": "Pick some",
            "options": [{"id": "1", "label": "x"}],
            "multi": True,
            "allow_text": False,
        },
    ]
    assert read.others == {"q1": "q1_other"}


def test_answers_go_back_as_consts_a_list_or_words() -> None:
    read = forms.read(
        form(
            {
                "q1": {
                    "type": "string",
                    "title": "One",
                    "oneOf": [choice("1", "a"), choice("2", "b")],
                },
                "q1_other": {"type": "string"},
                "q2": {
                    "type": "array",
                    "title": "Some",
                    "items": {"anyOf": [choice("1", "x"), choice("2", "y")]},
                },
                "q3": {"type": "string", "title": "Words only"},
            }
        )
    )
    assert forms.accepted(read, {"q1": ["2"], "q2": ["1", "2", "9"], "q3": "free"}) == {
        "action": "accept",
        "content": {"q1": "2", "q2": ["1", "2"], "q3": "free"},
    }
    assert forms.accepted(read, {"q1": "my own"}) == {
        "action": "accept",
        "content": {"q1_other": "my own"},
    }
    assert forms.accepted(read, {}) == {"action": "accept", "content": {}}, "unanswered, left out"


def test_at_most_four_questions_of_eight_options() -> None:
    many = {
        f"q{n}": {
            "type": "string",
            "title": f"Question {n}",
            "oneOf": [choice(str(i), f"o{i}") for i in range(12)],
        }
        for n in range(1, 7)
    }
    read = forms.read(form(many))
    assert [q["id"] for q in read.questions] == ["q1", "q2", "q3", "q4"]
    assert len(read.questions[0]["options"]) == 8


def test_a_form_with_nothing_to_show_is_refused() -> None:
    with pytest.raises(ValueError):
        forms.read(form({"name": {"type": "string", "title": "Not a question field"}}))


def walk(ledger: Ledger, items: list[tuple[str, Any]]) -> list[tuple[str, str]]:
    walker = Replay(ledger)
    rows: list[replay.Row] = []
    for kind, value in items:
        if kind == "user":
            rows += walker.user({"type": "text", "text": value})
        elif kind == "agent":
            rows += walker.agent(value)
        else:
            rows += walker.tool({"toolCallId": value})
    rows += walker.finish()
    return [(row.kind, row.text or str(row.update.get("toolCallId") or "")) for row in rows]


CONVERSATION = [
    ("user", "One."),
    ("agent", "Reply."),
    ("tool", "call_A"),
    ("agent", "STARTED"),
    ("agent", "\n\nFINISHED"),
    ("user", "Steered."),
    ("tool", "call_B"),
    ("agent", "End."),
]


def test_a_conversation_the_device_published_whole_replays_nothing() -> None:
    ledger = Ledger()
    ledger.note_text(USER)
    ledger.note_text(AGENT)
    ledger.note_tool("call_A")
    ledger.note_text(AGENT)
    ledger.note_text(AGENT)
    ledger.note_text(USER)
    ledger.note_tool("call_B")
    ledger.note_text(AGENT)
    assert walk(ledger, CONVERSATION) == []


def test_what_another_client_added_is_published_and_counted() -> None:
    ledger = Ledger()
    ledger.note_text(USER)
    ledger.note_text(AGENT)
    ledger.note_tool("call_A")
    ledger.note_text(AGENT)
    assert walk(ledger, CONVERSATION) == [
        ("agent", "FINISHED"),
        ("user", "Steered."),
        ("tool", "call_B"),
        ("agent", "End."),
    ]
    assert walk(ledger, CONVERSATION) == [], "and only once"
    assert ledger.anchor == "call_B"


def test_a_message_in_several_parts_is_one_row() -> None:
    walker = Replay(Ledger())
    walker.user({"type": "text", "text": "/plan"})
    walker.user({"type": "text", "text": " Task: add a file."})
    walker.user({"type": "resource_link", "uri": "file:///x/y/z.bin", "size": 3})
    [row] = walker.agent("Planned.")[:1]
    assert (row.kind, row.text) == ("user", "/plan Task: add a file.")
    assert row.attachments == [{"name": "z.bin", "mime": "application/octet-stream", "size": 3}]


def test_the_ledger_is_kept_with_the_session(tmp_path: Any) -> None:
    from rc_client.registry import Registry

    registry = Registry(tmp_path / "state.sqlite3")
    ledger = Ledger(tools={"a"}, anchor="a", counts={"": [1, 2]}, wrote=5)
    ledger.save(registry, "s-1")
    assert Ledger.load(registry, "s-1") == ledger
    assert Ledger.load(registry, "s-2") == Ledger()
    registry.close()
