"""What the backend is busy with: responses in flight, tools being answered, and their timings."""

from __future__ import annotations

from typing import Any

import pytest

from rc_gateway.live.usage import MAX_TRACKED_RESPONSES
from rc_gateway.live.work import CONTINUATION_SECONDS, STALE_RESPONSE_SECONDS, WorkTracker


def event(kind: str, response: str | None = None) -> dict[str, Any]:
    inner: dict[str, Any] = {"type": kind}
    if response is not None:
        inner["response"] = {"id": response}
    return inner


def test_a_response_is_work_from_its_first_event_until_its_final_one() -> None:
    work = WorkTracker()
    assert not work.working(0.0)
    work.note_event(event("response.created", "r1"), 1.0)
    assert work.working(1.0)
    work.note_event(event("response.in_progress", "r1"), 1.1)
    assert work.working(2.0)
    work.note_event(event("response.completed", "r1"), 3.5)
    assert not work.working(3.5)


@pytest.mark.parametrize("final", ["response.completed", "response.incomplete", "response.failed"])
def test_every_way_a_response_can_end_ends_the_work_and_times_it(final: str) -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 10.0)
    work.note_event(event(final, "r1"), 12.5)
    assert not work.working(12.5)
    assert work.record()["backend_ms_max"] == 2500


def test_overlapping_responses_are_work_until_the_last_one_ends() -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 0.0)
    work.note_event(event("response.created", "r2"), 1.0)
    work.note_event(event("response.completed", "r1"), 2.0)
    assert work.working(2.0)
    work.note_event(event("response.completed", "r2"), 4.0)
    assert not work.working(4.0)
    assert work.record() == {"backend_ms_median": 2500, "backend_ms_max": 3000, "tool_ms_max": None}


def test_a_response_is_timed_from_its_first_event_not_its_latest() -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 1.0)
    work.note_event(event("response.output_item.added", "r1"), 2.0)
    work.note_event(event("response.completed", "r1"), 4.0)
    assert work.record()["backend_ms_median"] == 3000


def test_a_response_whose_start_was_never_seen_is_over_but_not_timed() -> None:
    work = WorkTracker()
    work.note_event(event("response.completed", "r1"), 5.0)
    assert not work.working(5.0)
    assert work.record()["backend_ms_median"] is None
    # Nor does a late event of it start it again.
    work.note_event(event("response.in_progress", "r1"), 6.0)
    assert not work.working(6.0)


def test_a_repeated_final_event_or_a_late_one_changes_nothing() -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 0.0)
    work.note_event(event("response.completed", "r1"), 2.0)
    work.note_event(event("response.completed", "r1"), 9.0)
    work.note_event(event("response.output_item.done", "r1"), 9.5)
    assert not work.working(9.5)
    assert work.backend.count == 1


def test_events_that_name_no_response_are_attributed_to_none() -> None:
    work = WorkTracker()
    work.note_event(event("response.output_item.added"), 0.0)
    work.note_event({"type": "response.created", "response": "r1"}, 0.0)
    work.note_event({"type": "response.created", "response": {"id": 7}}, 0.0)
    work.note_event({"type": "response.created", "response": {"id": ""}}, 0.0)
    assert not work.working(0.0)


def test_a_tool_is_work_until_its_answer_has_been_handed_back() -> None:
    work = WorkTracker()
    work.tool_started()
    assert work.working(0.0)
    work.tool_ran(0.8)
    assert work.working(1.0)
    work.tool_ended()
    assert not work.working(1.0)
    assert work.record()["tool_ms_max"] == 800


def test_a_tool_that_failed_to_answer_still_stops_being_work() -> None:
    work = WorkTracker()
    work.tool_started()
    work.tool_ended()
    work.tool_ended()  # an end too many never drives the count below zero
    work.tool_started()
    assert work.working(0.0)


def test_the_response_a_tools_answer_asked_for_is_awaited_so_the_hint_does_not_blink() -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 0.0)
    work.tool_started()
    work.note_event(event("response.completed", "r1"), 0.2)
    assert work.working(0.3)  # the tool is still running
    work.continuation_asked(1.0)
    work.tool_ended()
    assert work.working(1.1)  # nothing is in flight, but a response was asked for
    work.note_event(event("response.created", "r2"), 1.4)
    assert work.working(2.0)
    work.note_event(event("response.completed", "r2"), 3.0)
    assert not work.working(3.0)  # and the wait is over: it began


def test_the_final_events_of_the_response_a_tool_was_called_by_do_not_end_the_wait() -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 0.0)
    work.tool_started()
    work.continuation_asked(0.1)  # a quick tool: answered before r1's own end was heard
    work.tool_ended()
    work.note_event(event("response.completed", "r1"), 0.2)
    assert work.working(0.3)
    work.note_event(event("response.created", "r2"), 0.5)
    work.note_event(event("response.completed", "r2"), 1.0)
    assert not work.working(1.0)


def test_the_wait_for_the_next_response_ends_after_ten_seconds() -> None:
    work = WorkTracker()
    work.continuation_asked(100.0)
    assert work.working(100.0 + CONTINUATION_SECONDS - 0.1)
    assert not work.working(100.0 + CONTINUATION_SECONDS)


def test_a_response_that_never_ends_stops_being_work_but_is_timed_if_it_does_end() -> None:
    work = WorkTracker()
    work.note_event(event("response.created", "r1"), 0.0)
    assert work.working(STALE_RESPONSE_SECONDS - 0.1)
    assert not work.working(STALE_RESPONSE_SECONDS)
    work.note_event(event("response.completed", "r1"), 90.0)
    assert work.record()["backend_ms_max"] == 90_000


def test_the_longest_tool_run_is_kept() -> None:
    work = WorkTracker()
    for seconds in (0.05, 1.2, 0.3):
        work.tool_ran(seconds)
    assert work.record()["tool_ms_max"] == 1200


def test_how_many_responses_are_tracked_is_bounded() -> None:
    work = WorkTracker()
    for index in range(MAX_TRACKED_RESPONSES + 50):
        work.note_event(event("response.created", f"r{index}"), 0.0)
    assert len(work._flying) == MAX_TRACKED_RESPONSES
    for index in range(MAX_TRACKED_RESPONSES + 50):
        work.note_event(event("response.completed", f"r{index}"), 1.0)
    assert len(work._finished) == MAX_TRACKED_RESPONSES


@pytest.mark.parametrize("kind", [["response.completed"], {"a": 1}, None, 7, 1.5, True])
def test_an_event_type_that_is_not_text_is_a_response_event_that_ends_nothing(kind: Any) -> None:
    work = WorkTracker()
    work.note_event({"type": kind, "response": {"id": "r1"}}, 1.0)
    assert work.working(1.0)  # it names a response, and nothing in it says the response is over
    work.note_event(event("response.completed", "r1"), 2.0)
    assert not work.working(2.0)
    assert work.record()["backend_ms_max"] == 1000
