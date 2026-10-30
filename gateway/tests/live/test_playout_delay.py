"""The browser's playout delay, taken into the assistant's voice.

The voice is heard until the browser has played it, which is later than the gateway sent it. A voice
of 200 ms is sent at once, so the playhead puts its end 0.2 s after it was sent. The browser holds
it for its delay before it plays it, and the assistant counts as speaking for the half second's
pause besides: until 0.2 + delay + 0.5 s. Each test names the delay the browser reported (``None``
for none, which assumes the default of 0.4 s) and how long after the voice was sent the thing under
test happens.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.live.config import LiveConfig

from .fakes import FakeClock, pcm_ms, until
from .harness import (
    CUT_IN_FRAMES,
    PATIENT,
    Harness,
    audio,
    barge_in_lines,
    confirm,
    person_says,
    person_speaks,
    person_stops,
    prepared,
    read_back_sent,
    start,
    usage_records,
)

#: (the delay the browser reported, seconds after the voice was sent, whether it is still heard).
#: The voice is heard until 1.1 s with the assumed delay, 0.7 s with none, and 1.7 s with a second.
TIMELINE = [
    (None, 1.0, True),
    (None, 1.2, False),
    (0, 0.6, True),
    (0, 0.8, False),
    (1000, 1.6, True),
    (1000, 1.8, False),
]


def report(h: Harness, delay_ms: int | None) -> None:
    """The browser's playback report, with the delay it holds frames for (none: it says nothing)."""
    if delay_ms is not None:
        h.browser.command(type="playback", underruns=0, gap_ms=0, restarts=0, delay_ms=delay_ms)


async def voice_sent(h: Harness) -> None:
    h.upstream.push(audio(pcm_ms(200)))
    await until(lambda: h.browser.audio_ms() >= 200)


@pytest.mark.parametrize(("delay_ms", "after", "heard"), TIMELINE)
async def test_the_state_says_assistant_until_the_browser_has_played_the_voice(
    tmp_path: Path, delay_ms: int | None, after: float, heard: bool
) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    report(h, delay_ms)
    await voice_sent(h)
    h.browser.mic(b"\x00" * 960)
    await until(lambda: [m["speaker"] for m in h.browser.of("state")] == ["assistant"])
    clock.advance(after)
    h.browser.mic(b"\x00" * 960)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 2)
    speakers = [m["speaker"] for m in h.browser.of("state")]
    assert speakers == (["assistant"] if heard else ["assistant", "none"])
    await h.stop()


@pytest.mark.parametrize(("delay_ms", "after", "heard"), TIMELINE)
async def test_a_person_speaking_while_the_browser_is_still_playing_the_voice_may_interrupt_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    delay_ms: int | None,
    after: float,
    heard: bool,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    report(h, delay_ms)
    await voice_sent(h)  # all of it went to the browser: nothing is held back
    clock.advance(after)
    await person_speaks(h, frames=CUT_IN_FRAMES)
    h.browser.command(type="hangup")
    await h.finished()  # whatever was suspected is written down by the end of the call
    # Only speech begun while the voice was still being heard is taken for a possible
    # interruption, and each one leaves its line.
    assert len(barge_in_lines(caplog)) == int(heard)


#: The same, for a read-back of 2.2 s: `after` counts from when the clock stands 1 s into it, which
#: is 1.2 s before the playhead's end of it, so it is heard until 1.2 + delay + 0.5 s from then.
READ_BACK_TIMELINE = [
    (None, 2.0, True),
    (None, 2.2, False),
    (0, 1.6, True),
    (0, 1.8, False),
    (1000, 2.6, True),
    (1000, 2.8, False),
]


@pytest.mark.parametrize(("delay_ms", "after", "heard"), READ_BACK_TIMELINE)
async def test_the_confirm_gate_counts_the_voice_as_heard_until_the_browser_has_played_it(
    tmp_path: Path, delay_ms: int | None, after: float, heard: bool
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    report(h, delay_ms)
    await read_back_sent(h, clock)
    clock.advance(after)
    await person_says(h, "Yes.")
    if heard:
        # They spoke while it was still being heard: no answer to it.
        assert (await confirm(h, draft_id, "c2"))["error"] == "not_confirmed"
    else:
        sending = asyncio.create_task(confirm(h, draft_id, "c2"))
        await h.rig.answer_device()
        assert (await sending)["sent"] is True
    await h.stop()


@pytest.mark.parametrize(
    ("delay_ms", "after", "idle"),
    [
        (None, 1.05, False),
        (None, 1.2, True),
        (0, 1.05, True),
        (1000, 1.2, False),
        (1000, 1.8, True),
    ],
)
async def test_the_call_is_not_idle_while_the_browser_is_still_playing_the_voice(
    tmp_path: Path, delay_ms: int | None, after: float, idle: bool
) -> None:
    clock = FakeClock()
    config = LiveConfig(api_key="k", idle_seconds=1.0, max_call_seconds=600)
    h = await start(tmp_path, clock=clock, config=config)
    report(h, delay_ms)
    await voice_sent(h)
    clock.advance(after)
    if idle:
        await h.finished()
        assert h.browser.ended == "idle"
    else:
        await asyncio.sleep(0.1)
        assert h.call.active
        await h.stop()


@pytest.mark.parametrize(("delay_ms", "timed"), [(None, False), (0, True)])
async def test_an_assistant_the_browser_is_still_playing_is_not_timed_as_answering(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, delay_ms: int | None, timed: bool
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    config = LiveConfig(api_key="k", barge_in=False, idle_seconds=10_000, max_call_seconds=600)
    h = await start(tmp_path, clock=clock, config=config)
    report(h, delay_ms)
    await voice_sent(h)  # heard until 1.1 s with the assumed delay, until 0.7 s with none
    clock.advance(0.3)
    await person_speaks(h, frames=CUT_IN_FRAMES)  # over it, which nothing cuts off
    clock.advance(0.5)  # 0.8 s after the voice was sent
    await person_stops(h)
    clock.advance(0.7)
    h.upstream.push(audio(pcm_ms(200)))
    await until(lambda: h.browser.audio_ms() >= 400)
    h.browser.command(type="hangup")
    await h.finished()
    [record] = usage_records(caplog)
    assert record["replies"] == int(timed)


async def test_a_report_sets_the_delay_even_when_its_counters_are_bad(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.browser.command(type="playback", underruns=-1, gap_ms=0, restarts=0, delay_ms=900)
    await until(lambda: h.call._playback.pacer.playout_delay == pytest.approx(0.9))
    assert h.call.active
    await h.stop()


@pytest.mark.parametrize("junk", [-1, 0.4, "400", True, None])
async def test_a_bad_delay_leaves_the_assumed_one_in_force(tmp_path: Path, junk: Any) -> None:
    h = await start(tmp_path)
    before = h.call._playback.pacer.playout_delay
    h.browser.command(type="playback", underruns=0, gap_ms=0, restarts=0, delay_ms=junk)
    h.browser.mic(b"\x00" * 960)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 1)
    assert h.call._playback.pacer.playout_delay == before == pytest.approx(0.4)
    assert h.call.active
    await h.stop()


async def test_a_delay_longer_than_two_seconds_is_clamped(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.browser.command(type="playback", underruns=0, gap_ms=0, restarts=0, delay_ms=60_000)
    await until(lambda: h.call._playback.pacer.playout_delay == pytest.approx(2.0))
    await h.stop()


async def test_the_latest_delay_is_in_the_usage_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.browser.command(type="playback", underruns=0, gap_ms=0, restarts=0, delay_ms=400)
    h.browser.command(type="playback", underruns=2, gap_ms=300, restarts=2, delay_ms=800)
    h.browser.command(type="hangup")
    await h.finished()
    [record] = usage_records(caplog)
    assert record["browser_delay_ms"] == 800
    assert (record["browser_underruns"], record["browser_gap_ms"]) == (2, 300)
