"""Where a call's time went, as the usage line says it: pacing, first word, backend and tools."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.live.config import LiveConfig
from rc_gateway.live.stats import REPLY_WINDOW_SECONDS

from .fakes import QUIET, FakeClock, pcm_ms, until
from .harness import (
    CUT_IN_FRAMES,
    PATIENT,
    Harness,
    audio,
    backend_event,
    function_call,
    model_stops,
    person_speaks,
    person_stops,
    start,
    usage_records,
)

#: The voice goes ahead of playback in frames of 40 ms until the lead is full: 38 of them.
FIRST_LEAD_MS = 1520


async def hang_up(h: Harness, caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    h.browser.command(type="hangup")
    await h.finished()
    [record] = usage_records(caplog)
    return record


# ---- pacing ----


async def test_audio_that_came_after_the_queue_ran_dry_is_counted_as_starvation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    h.upstream.push(audio(pcm_ms(500)))
    await until(lambda: h.browser.audio_ms() >= 500)
    clock.advance(1.0)  # all of it was played out half a second ago
    h.upstream.push(audio(pcm_ms(100)))
    await until(lambda: h.browser.audio_ms() >= 600)
    clock.advance(0.12)  # the 100 ms was played out 20 ms ago: jitter, not a gap
    h.upstream.push(audio(pcm_ms(100)))
    await until(lambda: h.browser.audio_ms() >= 700)
    clock.advance(10.0)  # a pause this long is a new reply, not the model running late
    h.upstream.push(audio(pcm_ms(100)))
    await until(lambda: h.browser.audio_ms() >= 800)
    record = await hang_up(h, caplog)
    assert (record["starved"], record["starved_ms"], record["starved_max_ms"]) == (1, 500, 500)


async def test_audio_cut_off_by_an_interruption_is_counted_as_discarded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, clock=FakeClock(), config=PATIENT)
    h.upstream.push(audio(pcm_ms(3000)))
    await until(lambda: h.browser.audio_ms() >= FIRST_LEAD_MS)
    await person_speaks(h, frames=CUT_IN_FRAMES)
    await model_stops(h)  # its quiet queues behind what is still held
    assert h.browser.of("flush") != []
    h.upstream.push(audio(pcm_ms(500)))  # said while the person is talking: thrown away
    await h.upstream.settled()
    record = await hang_up(h, caplog)
    assert record["interruptions"] == 1
    # What was still held and what came after, but not the quiet that said it stopped.
    assert record["discarded_audio_ms"] == (3000 - FIRST_LEAD_MS) + 500
    assert (record["dropped_audio_ms"], record["starved"]) == (0, 0)


async def test_audio_that_backed_up_past_the_bound_is_counted_as_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, clock=FakeClock(), config=PATIENT)
    for _ in range(4):
        h.upstream.push(audio(pcm_ms(10_000)))
    await h.upstream.settled()
    record = await hang_up(h, caplog)
    assert 8_000 <= record["dropped_audio_ms"] <= 10_000
    assert record["discarded_audio_ms"] == 0


# ---- the first word ----


async def test_the_first_word_is_timed_from_the_end_of_the_persons_speech(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    await person_speaks(h)
    clock.advance(0.5)
    await person_stops(h)  # their last loud frame was at the time they began, 0.5 s ago
    clock.advance(0.9)
    h.upstream.push(audio(pcm_ms(200)))
    await until(lambda: h.browser.audio_ms() >= 200)

    clock.advance(1.0)  # the same voice going on, or a second reply, is not another answer
    h.upstream.push(audio(pcm_ms(200)))
    await until(lambda: h.browser.audio_ms() >= 400)

    await person_speaks(h, frames=CUT_IN_FRAMES)  # over it, and it stops for them
    await model_stops(h)
    clock.advance(0.5)
    await person_stops(h)
    clock.advance(2.1)
    h.upstream.push(audio(pcm_ms(200)))
    await until(lambda: h.browser.audio_ms() >= 600)
    record = await hang_up(h, caplog)
    # 1.4 s after the first utterance's last loud frame, and 0.5 + 2.1 s after the second's.
    assert record["replies"] == 2
    assert record["reply_ms_median"] == 2000
    assert record["reply_ms_max"] == 2600


async def test_a_voice_that_comes_too_late_is_no_answer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    await person_speaks(h)
    clock.advance(0.5)
    await person_stops(h)
    clock.advance(REPLY_WINDOW_SECONDS + 1)
    h.upstream.push(audio(pcm_ms(200)))
    await until(lambda: h.browser.audio_ms() >= 200)
    record = await hang_up(h, caplog)
    assert (record["replies"], record["reply_ms_median"], record["reply_ms_max"]) == (0, None, None)


async def test_the_person_speaking_again_before_the_assistant_answers_is_not_timed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    await person_speaks(h)
    clock.advance(0.5)
    await person_stops(h)
    clock.advance(0.6)
    await person_speaks(h)  # they were only pausing
    h.upstream.push(audio(pcm_ms(200)))  # and the assistant's voice is discarded or too early
    await h.upstream.settled()
    record = await hang_up(h, caplog)
    assert record["replies"] == 0


async def test_an_assistant_that_is_still_speaking_when_the_person_stops_is_not_timed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    config = LiveConfig(api_key="k", barge_in=False, idle_seconds=10_000, max_call_seconds=600)
    h = await start(tmp_path, clock=clock, config=config)
    h.upstream.push(audio(pcm_ms(3000)))
    await until(lambda: h.browser.audio_ms() >= FIRST_LEAD_MS)
    await person_speaks(h, frames=CUT_IN_FRAMES)  # talking over it, and it does not stop
    clock.advance(0.5)
    await person_stops(h)
    clock.advance(0.2)
    h.upstream.push(audio(pcm_ms(200)))
    await h.upstream.settled()
    record = await hang_up(h, caplog)
    assert record["replies"] == 0


# ---- what was heard from the person ----


async def test_how_long_the_person_spoke_and_how_much_was_transcribed_are_in_the_usage_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    await person_speaks(h)  # eight loud frames: speech begins at the fifth, so four are speech
    clock.advance(0.1)
    h.browser.mic(QUIET)
    h.browser.mic(QUIET)  # inside the hangover: still speech
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 11)
    clock.advance(0.5)
    await person_stops(h)  # the detector ends the speech here, and this frame is not speech
    for words in ("what", "is", "running"):
        h.upstream.push({"type": "session.input_transcript.delta", "delta": words})
    h.upstream.push({"type": "session.output_transcript.delta", "delta": "Two sessions."})
    await h.upstream.settled()
    record = await hang_up(h, caplog)
    assert record["speech_ms"] == 6 * 20
    assert record["input_transcripts"] == 3


async def test_a_call_in_which_the_person_said_nothing_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, clock=FakeClock(), config=PATIENT)
    for _ in range(20):
        h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 20)
    h.upstream.push(audio(pcm_ms(1500)))  # and the assistant said something to nobody
    await until(lambda: h.browser.audio_ms() >= 1500)
    record = await hang_up(h, caplog)
    assert (record["speech_ms"], record["input_transcripts"], record["replies"]) == (0, 0, 0)


# ---- the backend and the tools ----


async def test_backend_responses_and_tools_are_timed_in_the_usage_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    gate = asyncio.Event()
    original = h.rig.tools.run

    async def slow(name: str, raw: str) -> str:
        await gate.wait()
        return await original(name, raw)

    h.rig.tools.run = slow  # type: ignore[method-assign,assignment]

    h.upstream.push(backend_event("response.created", "r1"))
    await until(lambda: len(h.browser.of("working")) == 1)
    clock.advance(2.5)  # the backend thinks, then calls a tool and ends that response
    h.upstream.push(function_call("list_sessions", {}))
    h.upstream.push(backend_event("response.completed", "r1"))
    await h.upstream.settled()
    clock.advance(1.2)  # the tool takes this long
    gate.set()
    await until(lambda: "response.create" in h.upstream.types())
    await h.upstream.settled()
    clock.advance(0.3)
    h.upstream.push(backend_event("response.created", "r2"))
    await h.upstream.settled()
    clock.advance(1.0)
    h.upstream.push(backend_event("response.incomplete", "r2"))
    await until(lambda: len(h.browser.of("working")) == 2)
    record = await hang_up(h, caplog)
    assert record["backend_ms_median"] == 1750  # 2.5 s and 1.0 s
    assert record["backend_ms_max"] == 2500
    assert record["tool_ms_max"] == 1200
