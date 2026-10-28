"""The "working" hint: a backend response in flight, or a tool being answered, until neither is."""

from __future__ import annotations

import asyncio
from pathlib import Path

from rc_gateway.live.work import CONTINUATION_SECONDS

from .fakes import FakeClock, until
from .harness import PATIENT, Harness, backend_event, function_call, start

ON = {"type": "working", "on": True}
OFF = {"type": "working", "on": False}


async def gated_tools(h: Harness) -> asyncio.Event:
    """Make every tool wait for the returned event before it runs."""
    gate = asyncio.Event()
    original = h.rig.tools.run

    async def slow(name: str, raw: str) -> str:
        await gate.wait()
        return await original(name, raw)

    h.rig.tools.run = slow  # type: ignore[method-assign,assignment]
    return gate


async def test_working_is_on_while_a_backend_response_is_in_flight(tmp_path: Path) -> None:
    h = await start(tmp_path)
    assert h.browser.of("working") == []
    h.upstream.push(backend_event("response.created", "r1"))
    await until(lambda: h.browser.of("working") == [ON])
    h.upstream.push(backend_event("response.completed", "r1", output=[]))
    await until(lambda: h.browser.of("working") == [ON, OFF])
    await h.stop()


async def test_it_is_sent_only_when_it_changes(tmp_path: Path) -> None:
    h = await start(tmp_path)
    for kind, response in (
        ("response.created", "r1"),
        ("response.in_progress", "r1"),
        ("response.created", "r2"),
    ):
        h.upstream.push(backend_event(kind, response))
    await until(lambda: h.browser.of("working") == [ON])
    h.upstream.push(backend_event("response.completed", "r1"))
    await h.upstream.settled()
    assert h.browser.of("working") == [ON]  # r2 is still in flight
    h.upstream.push(backend_event("response.failed", "r2"))
    await until(lambda: h.browser.of("working") == [ON, OFF])
    h.upstream.push(backend_event("response.completed", "r2"))
    await h.upstream.settled()
    assert h.browser.of("working") == [ON, OFF]
    await h.stop()


async def test_a_tool_run_is_work_and_so_is_the_wait_for_the_response_that_follows(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path)
    gate = await gated_tools(h)
    h.upstream.push(backend_event("response.created", "r1"))
    h.upstream.push(function_call("list_sessions", {}))
    h.upstream.push(backend_event("response.completed", "r1"))  # it only called the tool
    await h.upstream.settled()
    assert h.browser.of("working") == [ON]  # the tool is running

    gate.set()
    await until(lambda: "response.create" in h.upstream.types())
    await h.upstream.settled()
    assert h.browser.of("working") == [ON]  # and no blink while its answer is on the way back

    h.upstream.push(backend_event("response.created", "r2"))
    h.upstream.push(backend_event("response.completed", "r2"))
    await until(lambda: h.browser.of("working") == [ON, OFF])
    await h.stop()


async def test_tools_that_overlap_are_one_stretch_of_work(tmp_path: Path) -> None:
    h = await start(tmp_path)
    gate = await gated_tools(h)
    h.upstream.push(function_call("list_sessions", {}, "call_a"))
    h.upstream.push(function_call("list_pending_decisions", {}, "call_b"))
    await until(lambda: h.browser.of("working") == [ON])
    gate.set()
    await until(lambda: h.upstream.types().count("response.create") == 2)
    await h.upstream.settled()
    assert h.browser.of("working") == [ON]
    h.upstream.push(backend_event("response.created", "r2"))
    h.upstream.push(backend_event("response.completed", "r2"))
    await until(lambda: h.browser.of("working") == [ON, OFF])
    await h.stop()


async def test_a_tool_whose_next_response_never_comes_stops_being_work_after_ten_seconds(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    h.upstream.push(function_call("list_sessions", {}))
    await until(lambda: "response.create" in h.upstream.types())
    await h.upstream.settled()
    assert h.browser.of("working") == [ON]
    clock.advance(CONTINUATION_SECONDS + 1)
    await until(lambda: h.browser.of("working") == [ON, OFF])
    await h.stop()


async def test_a_refused_tool_is_work_like_any_other(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push(function_call("delete_everything", {}))
    await until(lambda: h.browser.of("working") == [ON])
    await until(lambda: "response.create" in h.upstream.types())
    await h.stop()


async def test_events_that_name_no_response_start_no_work(tmp_path: Path) -> None:
    h = await start(tmp_path)
    for event in (
        {"type": "response.event", "event": "text"},
        {"type": "response.event", "event": {"type": "response.created"}},
        {"type": "response.event", "event": {"type": "response.output_item.added", "item": {}}},
        {"type": "response.event"},
    ):
        h.upstream.push(event)
    await h.upstream.settled()
    assert h.browser.of("working") == []
    assert h.call.active
    await h.stop()


async def test_a_nested_event_whose_type_is_not_text_does_not_end_the_call(tmp_path: Path) -> None:
    h = await start(tmp_path)
    for kind in (["response.completed"], {"type": "response.completed"}, 7, None):
        h.upstream.push(
            {
                "type": "response.event",
                "event": {"type": kind, "response": {"id": "r1", "usage": {"input_tokens": 1}}},
            }
        )
    await h.upstream.settled()
    assert h.call.active
    # The call goes on counting work, and a proper final event still ends it.
    h.upstream.push(backend_event("response.completed", "r1"))
    await until(lambda: h.browser.of("working") == [ON, OFF])
    await h.stop()
