"""A call and its meter: started with the call, ticked with its open time, able to end it.

The meter is stopped once, with the seconds the call was open, however the call ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.live.protocol import close_code_for

from .fakes import FakeBrowser, FakeClock, FakeUpstream, until
from .harness import CONFIG, PATIENT, Harness, first, start, usage_records

pytestmark = pytest.mark.timeout(20)


class FakeMeter:
    """A meter a test drives: it remembers what the call told it, and says what it is told to."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.ticks: list[float] = []
        self.stopped: list[float] = []
        self.reason: str | None = None
        self.billing: str | None = None
        self.stop_error: Exception | None = None
        self.hold_answer = False
        self.answer = asyncio.Event()

    def start(self, call_id: str) -> None:
        self.started.append(call_id)

    def tick(self, seconds: float) -> None:
        self.ticks.append(seconds)

    def verdict(self) -> str | None:
        return self.reason

    def stop(self, seconds: float) -> None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped.append(seconds)

    async def settled(self) -> None:
        if self.hold_answer:
            await self.answer.wait()

    def record(self) -> dict[str, Any]:
        return {"billed_seconds": int(sum(self.stopped)), "billing": self.billing}


# ---- starting and ticking ----


async def test_the_meter_is_started_once_with_the_calls_id(tmp_path: Path) -> None:
    meter = FakeMeter()
    h = await start(tmp_path, meter=meter)
    await first(h, "ready")
    assert meter.started == [h.call.call_id]
    await h.stop()
    assert meter.started == [h.call.call_id]


async def test_the_meter_is_ticked_with_the_seconds_the_call_has_been_open(tmp_path: Path) -> None:
    clock = FakeClock()
    meter = FakeMeter()
    h = await start(tmp_path, clock=clock, config=PATIENT, meter=meter)
    await first(h, "ready")
    clock.advance(7)
    await until(lambda: bool(meter.ticks) and meter.ticks[-1] == 7)
    clock.advance(23)
    await until(lambda: meter.ticks[-1] == 30)
    await h.stop()


# ---- ending the call ----


async def test_a_verdict_ends_the_call_with_its_reason(tmp_path: Path) -> None:
    meter = FakeMeter()
    h = await start(tmp_path, meter=meter)
    await first(h, "ready")
    meter.reason = "quota_exceeded"
    await h.finished()
    assert h.browser.ended == "quota_exceeded"
    assert h.call.end_reason == "quota_exceeded"
    assert h.upstream.closed
    assert len(meter.stopped) == 1


async def test_a_verdict_wins_over_a_limit_reached_at_the_same_moment(tmp_path: Path) -> None:
    clock = FakeClock()
    meter = FakeMeter()
    h = await start(tmp_path, clock=clock, config=CONFIG, meter=meter)
    await first(h, "ready")
    meter.reason = "subscription_required"
    clock.advance(601)
    await h.finished()
    assert h.browser.ended == "subscription_required"


def _hang_up(h: Harness, clock: FakeClock) -> None:
    h.browser.command(type="hangup")


def _go_idle(h: Harness, clock: FakeClock) -> None:
    clock.advance(61)


def _reach_the_limit(h: Harness, clock: FakeClock) -> None:
    clock.advance(601)


def _lose_the_service(h: Harness, clock: FakeClock) -> None:
    h.upstream.drop()


def _lose_the_browser(h: Harness, clock: FakeClock) -> None:
    h.browser.close()


def _stop_the_gateway(h: Harness, clock: FakeClock) -> None:
    h.call.request_finish("gateway_stopping")


ENDINGS: dict[str, tuple[Callable[[Harness, FakeClock], None], str]] = {
    "hangup": (_hang_up, "hangup"),
    "idle": (_go_idle, "idle"),
    "max duration": (_reach_the_limit, "max_duration"),
    "the service lost": (_lose_the_service, "upstream_lost"),
    "the browser closed": (_lose_the_browser, "browser_closed"),
    "the gateway stopping": (_stop_the_gateway, "gateway_stopping"),
}


@pytest.mark.parametrize("ending", ENDINGS)
async def test_the_meter_is_stopped_once_with_the_open_seconds_however_the_call_ends(
    tmp_path: Path, ending: str
) -> None:
    clock = FakeClock()
    meter = FakeMeter()
    h = await start(tmp_path, clock=clock, config=CONFIG, meter=meter)
    await first(h, "ready")
    clock.advance(5)
    await until(lambda: bool(meter.ticks) and meter.ticks[-1] == 5)
    end, reason = ENDINGS[ending]
    end(h, clock)
    await h.finished()
    assert h.call.end_reason == reason
    assert meter.stopped == [h.call.duration()]
    assert meter.stopped[0] >= 5


async def test_a_call_that_ends_before_it_is_ready_still_stops_its_meter(tmp_path: Path) -> None:
    browser = FakeBrowser()
    browser.fail_sends = "browser_closed"
    meter = FakeMeter()
    h = await start(tmp_path, browser=browser, meter=meter)
    await h.finished()
    assert len(meter.stopped) == 1


# ---- what the meter cannot do to the end of a call ----


async def test_a_call_whose_ending_is_cut_short_still_stops_its_meter_and_logs_its_usage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    meter = FakeMeter()
    upstream = FakeUpstream(answer_close=False)  # the Live API never confirms the close
    h = await start(tmp_path, clock=clock, config=PATIENT, meter=meter, upstream=upstream)
    await first(h, "ready")
    clock.advance(7)
    h.call.request_finish("hangup")
    await until(lambda: h.browser.ended == "hangup")
    ending = h.call._finish_task
    assert ending is not None and not ending.done()  # still waiting for the Live API's close
    ending.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ending
    assert meter.stopped == [7.0]
    assert [record["billed_seconds"] for record in usage_records(caplog)] == [7]
    with contextlib.suppress(asyncio.CancelledError):
        await h.task


async def test_a_meter_that_fails_to_stop_does_not_keep_the_call_from_ending(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    meter = FakeMeter()
    meter.stop_error = RuntimeError("no")
    h = await start(tmp_path, meter=meter)
    await first(h, "ready")
    h.browser.command(type="hangup")
    await h.finished()
    assert h.browser.ended == "hangup"
    assert h.upstream.closed
    assert len(usage_records(caplog)) == 1
    assert "live meter failed to stop" in [r.getMessage() for r in caplog.records]


async def test_an_answer_that_never_comes_does_not_hold_the_end_of_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    meter = FakeMeter()
    meter.hold_answer = True
    h = await start(tmp_path, meter=meter, step_timeout=0.1)
    await first(h, "ready")
    h.browser.command(type="hangup")
    await h.finished()
    assert h.browser.ended == "hangup"
    assert len(usage_records(caplog)) == 1


async def test_the_browser_is_told_and_closed_before_the_meters_answer_is_waited_for(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    meter = FakeMeter()
    meter.hold_answer = True
    h = await start(tmp_path, meter=meter, step_timeout=10)
    await first(h, "ready")
    h.browser.command(type="hangup")
    await until(lambda: h.browser.ended == "hangup" and h.upstream.closed)
    assert not h.task.done()
    assert usage_records(caplog) == []
    meter.answer.set()
    await h.finished()
    assert len(usage_records(caplog)) == 1


# ---- the usage line ----


async def test_the_usage_line_carries_what_the_meter_reports(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    meter = FakeMeter()
    meter.billing = "billed"
    h = await start(tmp_path, clock=clock, config=PATIENT, meter=meter)
    await first(h, "ready")
    clock.advance(12)
    await h.stop()
    [record] = usage_records(caplog)
    assert record["billed_seconds"] == 12
    assert record["billing"] == "billed"
    assert record["seconds"] == 12


async def test_a_call_with_no_meter_bills_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await first(h, "ready")
    await h.stop()
    [record] = usage_records(caplog)
    assert (record["billed_seconds"], record["billing"]) == (0, None)


# ---- the close codes of what a call can cost ----


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("subscription_required", 4402),
        ("quota_exceeded", 4402),
        ("billing_not_configured", 4503),
    ],
)
def test_the_reasons_a_call_can_cost_have_their_own_close_codes(reason: str, code: int) -> None:
    assert close_code_for(reason) == code
