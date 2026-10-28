"""What OpenAI reports, as the meter keeps it: voice seconds replace, backend tokens add once."""

from __future__ import annotations

from typing import Any

import pytest

from rc_gateway.live.usage import UsageMeter, is_final_response


def completed(identifier: str | None, **usage: Any) -> dict[str, Any]:
    response: dict[str, Any] = {"output": [], "usage": usage}
    if identifier is not None:
        response["id"] = identifier
    return {"type": "response.completed", "response": response}


def test_voice_seconds_are_cumulative_snapshots_not_increments() -> None:
    meter = UsageMeter()
    meter.note_voice({"seconds": 12})
    meter.note_voice({"seconds": 20.5})
    assert meter.record()["voice_seconds"] == 20.5
    assert meter.record()["usage_final"] is False
    meter.note_voice({"seconds": 31}, final=True)
    assert meter.record()["voice_seconds"] == 31.0
    assert meter.record()["usage_final"] is True


def test_nothing_reported_is_recorded_as_nothing() -> None:
    meter = UsageMeter()
    meter.note_voice(None)
    meter.note_voice({"seconds": "many"})
    meter.note_voice({"seconds": True})
    assert meter.record()["voice_seconds"] is None


def test_each_backend_response_is_counted_once_by_its_id() -> None:
    meter = UsageMeter()
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 40},
        "output_tokens_details": {"reasoning_tokens": 5},
    }
    meter.note_response(completed("resp_1", **usage))
    meter.note_response(completed("resp_1", **usage))
    meter.note_response(completed("resp_2", input_tokens=50, output_tokens=10))
    assert meter.record() == {
        "voice_seconds": None,
        "usage_final": False,
        "backend_responses": 2,
        "backend_input_tokens": 150,
        "backend_output_tokens": 30,
        "backend_cached_tokens": 40,
        "backend_reasoning_tokens": 5,
    }


def test_events_that_are_not_a_finished_response_are_ignored() -> None:
    meter = UsageMeter()
    meter.note_response({"type": "response.created", "response": {"usage": {"input_tokens": 9}}})
    meter.note_response({"type": "response.completed", "response": {"id": "r"}})
    meter.note_response({"type": "response.completed", "response": "nope"})
    meter.note_response({"type": "response.output_item.done", "item": {}})
    assert meter.record()["backend_responses"] == 0


def test_an_incomplete_or_failed_response_still_used_tokens() -> None:
    meter = UsageMeter()
    for kind in ("response.incomplete", "response.failed"):
        meter.note_response(
            {
                "type": kind,
                "response": {"id": kind, "usage": {"input_tokens": 7, "output_tokens": 1}},
            }
        )
    assert meter.record()["backend_input_tokens"] == 14


def test_odd_token_values_never_break_the_meter() -> None:
    meter = UsageMeter()
    meter.note_response(completed("r", input_tokens="many", output_tokens=-3, extra=1))
    assert meter.record()["backend_input_tokens"] == 0
    assert meter.record()["backend_output_tokens"] == 0
    assert meter.record()["backend_responses"] == 1


NOT_TEXT: list[Any] = [["response.completed"], {"type": "response.completed"}, None, 7, 1.5, True]


@pytest.mark.parametrize("kind", NOT_TEXT)
def test_an_event_type_that_is_not_text_closes_nothing_and_raises_nothing(kind: Any) -> None:
    event = {"type": kind, "response": {"id": "r", "usage": {"input_tokens": 5}}}
    assert is_final_response(event) is False
    meter = UsageMeter()
    meter.note_response(event)
    assert meter.record()["backend_responses"] == 0


def test_only_the_three_final_event_types_close_a_response() -> None:
    kinds = ("response.completed", "response.incomplete", "response.failed")
    assert all(is_final_response({"type": kind}) for kind in kinds)
    for kind in ("response.created", "response.output_item.done", "", "RESPONSE.COMPLETED"):
        assert not is_final_response({"type": kind})
    assert not is_final_response({})
