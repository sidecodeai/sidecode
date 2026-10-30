"""Microphone frame validation, the output pacer and the interruption rules."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest

from rc_gateway.live.audio import MAX_FRAME_BYTES, MIN_FRAME_BYTES, MicFrameGate, mean_abs
from rc_gateway.live.barge_in import CONFIRM_WINDOW_SECONDS, YIELD_SILENCE_SECONDS, BargeIn
from rc_gateway.live.errors import LiveError
from rc_gateway.live.playback import (
    FRAME_BYTES,
    LEAD_SECONDS,
    PLAYOUT_DELAY_SECONDS,
    STARVATION_MIN_SECONDS,
    STARVATION_WINDOW_SECONDS,
    SUPPRESS_CAP_SECONDS,
    SUPPRESS_HOLD_SECONDS,
    VOICE_HANGOVER_SECONDS,
    OutputPacer,
    Playback,
)

from .fakes import FRAME_BYTES as MIC_FRAME_BYTES
from .fakes import LOUD, QUIET, FakeClock, noise_floor_ms, pcm_ms, until

BYTES_PER_SECOND = 48_000


def test_the_gate_takes_real_time_microphone_frames() -> None:
    clock = FakeClock()
    gate = MicFrameGate(clock=clock)
    for _ in range(500):
        gate.check(QUIET)
        clock.advance(0.02)


@pytest.mark.parametrize(
    "size",
    [0, 1, MIN_FRAME_BYTES - 2, MAX_FRAME_BYTES + 2, MIC_FRAME_BYTES + 1],
)
def test_the_gate_refuses_frames_of_the_wrong_size(size: int) -> None:
    with pytest.raises(LiveError) as refused:
        MicFrameGate(clock=FakeClock()).check(b"\x00" * size)
    assert refused.value.code == "protocol_error"


def test_the_gate_lets_a_stalled_tab_catch_up_but_not_flood() -> None:
    clock = FakeClock()
    gate = MicFrameGate(clock=clock)
    # A burst of one second of audio at once is a tab catching up.
    for _ in range(50):
        gate.check(QUIET)
    # Several seconds at once is not.
    with pytest.raises(LiveError):
        for _ in range(500):
            gate.check(QUIET)


def test_mean_abs_measures_amplitude() -> None:
    assert mean_abs(b"") == 0.0
    assert mean_abs(QUIET) == 0.0
    assert mean_abs(LOUD) == 4000.0
    assert mean_abs(LOUD + b"\x01") == 4000.0


# ---- the pacer ----


def test_the_browsers_queue_runs_a_second_and_a_half_ahead_of_playback() -> None:
    assert LEAD_SECONDS == 1.5
    pacer = OutputPacer()
    pacer.add(pcm_ms(5000), 0.0)
    assert sum(map(len, pacer.due(0.0))) / BYTES_PER_SECOND == pytest.approx(1.5, abs=0.04)
    # Half a second of playback later, half a second more is due: the queue stays that deep.
    assert sum(map(len, pacer.due(0.5))) / BYTES_PER_SECOND == pytest.approx(0.5, abs=0.04)


async def test_a_deep_queue_costs_an_interruption_one_flush_and_what_was_still_held() -> None:
    clock = FakeClock()
    sink = Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    task = asyncio.create_task(playback.run())
    playback.add(pcm_ms(4000))
    await until(lambda: sum(map(len, sink.frames)) >= 1.5 * BYTES_PER_SECOND)
    sent = len(sink.frames)

    dropped = await playback.interrupt()
    # The browser's flush stops what it has queued; the gateway drops the rest and sends nothing.
    assert sink.flushes == 1
    assert dropped == pytest.approx(2.5 * BYTES_PER_SECOND, abs=2 * FRAME_BYTES)
    await asyncio.sleep(0.1)
    assert len(sink.frames) == sent
    task.cancel()


def test_the_pacer_releases_a_lead_at_once_and_the_rest_at_the_speed_of_speech() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(4000), 0.0)
    first = pacer.due(0.0)
    assert sum(map(len, first)) / BYTES_PER_SECOND == pytest.approx(LEAD_SECONDS, abs=0.04)
    assert all(len(frame) <= FRAME_BYTES for frame in first)
    assert pacer.due(0.0) == []
    wait = pacer.next_wake(0.0)
    assert wait is not None and wait == pytest.approx(0.0, abs=0.05)

    # One second of the clock later, one more second of audio is due, and no more.
    sent = sum(map(len, first))
    for tick in range(1, 101):
        sent += sum(map(len, pacer.due(tick * 0.01)))
    assert sent / BYTES_PER_SECOND == pytest.approx(1.0 + LEAD_SECONDS, abs=0.05)


def test_the_pacer_keeps_every_byte_in_order_and_sample_aligned() -> None:
    pacer = OutputPacer()
    data = bytes(range(256)) * 40
    # Cut at odd places: a chunk that ends mid-sample hands its odd byte to the next.
    for start in range(0, len(data), 1001):
        pacer.add(data[start : start + 1001], 0.0)
    out = b""
    clock = 0.0
    while pacer.held_bytes:
        out += b"".join(pacer.due(clock))
        clock += 0.05
    out += b"".join(pacer.due(clock))
    assert len(out) % 2 == 0
    assert out == data[: len(out)]
    assert len(data) - len(out) <= 1


def test_the_pacer_forgets_held_audio_when_cleared() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(3000), 0.0)
    pacer.due(0.0)
    assert pacer.audible(0.1)
    dropped = pacer.clear(0.1)
    assert dropped > 0
    assert not pacer.audible(0.1)
    assert pacer.next_wake(0.1) is None


def test_past_its_bound_the_pacer_drops_the_oldest_audio_and_keeps_the_newest() -> None:
    pacer = OutputPacer(max_held_seconds=1.0)
    old, new = pcm_ms(900, level=1000), pcm_ms(600, level=2000)
    pacer.add(old, 0.0)
    assert pacer.dropped_bytes == 0
    assert pacer.add(new, 0.0) is True
    kept = pacer.held_bytes
    assert kept <= 1000 * 48
    # Whole frames go, so a little more than the 500 ms over the bound.
    assert 500 * 48 <= pacer.dropped_bytes < 500 * 48 + FRAME_BYTES
    # What is left is the end of the old audio and all of the new, in order.
    out = b""
    clock = 0.0
    while pacer.held_bytes:
        out += b"".join(pacer.due(clock))
        clock += 0.1
    assert out == (old + new)[-kept:]


def test_one_chunk_longer_than_the_bound_keeps_its_end() -> None:
    pacer = OutputPacer(max_held_seconds=1.0)
    pacer.add(pcm_ms(3000), 0.0)
    assert pacer.held_bytes == 1000 * 48
    assert pacer.dropped_bytes == 2000 * 48


def test_dropped_voice_is_no_longer_held() -> None:
    pacer = OutputPacer(
        max_held_seconds=0.5, classify=lambda frame, now: frame != bytes(len(frame))
    )
    pacer.add(pcm_ms(500), 0.0)
    assert pacer.audible(0.0)
    pacer.add(pcm_ms(500, level=0), 0.0)
    assert not pacer.audible(10.0)


def test_a_pause_in_the_model_restarts_the_lead_from_now() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(100), 0.0)
    pacer.due(0.0)
    pacer.add(pcm_ms(3000), 5.0)
    frames = pacer.due(5.0)
    assert sum(map(len, frames)) / BYTES_PER_SECOND == pytest.approx(LEAD_SECONDS, abs=0.04)


# ---- the interruption rules ----


class Sink:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.flushes = 0

    async def frame(self, data: bytes) -> None:
        self.frames.append(data)

    async def flush(self) -> None:
        self.flushes += 1


async def test_playback_sends_paced_frames_while_the_pacer_has_audio() -> None:
    sink = Sink()
    playback = Playback(sink.frame, sink.flush)
    task = asyncio.create_task(playback.run())
    playback.add(pcm_ms(300))
    await until(lambda: sum(map(len, sink.frames)) == 300 * 48)
    assert all(len(frame) <= FRAME_BYTES for frame in sink.frames)
    task.cancel()


async def test_an_interruption_drops_what_is_held_flushes_and_silences_the_model() -> None:
    clock = FakeClock()
    sink = Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    playback.add(pcm_ms(2000))
    assert playback.audible()

    dropped = await playback.interrupt()
    assert dropped == 2000 * 48
    assert sink.flushes == 1
    assert not playback.audible()
    assert playback.interruptions == 1

    # While the person talks, new audio from the model is thrown away.
    playback.add(pcm_ms(500))
    assert not playback.audible()
    assert playback.discarded_bytes == 2000 * 48 + 500 * 48


async def test_the_silence_holds_while_the_person_speaks_and_ends_after_the_hold() -> None:
    clock = FakeClock()
    sink = Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    await playback.interrupt()

    for _ in range(10):
        clock.advance(SUPPRESS_HOLD_SECONDS * 0.9)
        playback.person_speaking()
        playback.add(pcm_ms(40))
        assert not playback.audible()

    clock.advance(SUPPRESS_HOLD_SECONDS * 1.1)
    playback.add(pcm_ms(40))
    assert playback.audible()


async def test_the_silence_never_outlasts_its_cap() -> None:
    clock = FakeClock()
    sink = Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    await playback.interrupt()
    while clock.now < 1000.0 + SUPPRESS_CAP_SECONDS + 1:
        clock.advance(0.1)
        playback.person_speaking()
    playback.add(pcm_ms(40))
    assert playback.audible()


async def test_speech_without_an_interruption_silences_nothing() -> None:
    sink = Sink()
    playback = Playback(sink.frame, sink.flush, clock=FakeClock())
    playback.person_speaking()
    playback.add(pcm_ms(40))
    assert playback.audible()


async def test_frames_already_taken_are_not_sent_after_an_interruption() -> None:
    sent: list[bytes] = []
    flushed_after = []

    async def slow_frame(data: bytes) -> None:
        await asyncio.sleep(0.01)
        sent.append(data)

    async def flush() -> None:
        flushed_after.append(len(sent))

    playback = Playback(slow_frame, flush)
    task = asyncio.create_task(playback.run())
    playback.add(pcm_ms(2000))
    await until(lambda: len(sent) >= 2)
    await playback.interrupt()
    at_flush = len(sent)
    await asyncio.sleep(0.2)
    task.cancel()
    # At most the frame that was already being written when the interruption came.
    assert len(sent) <= at_flush + 1
    assert flushed_after == [at_flush]


# ---- a speech start over the assistant, and what the model shows it was ----


def barge_in_for(
    playback: Playback, clock: FakeClock, *, flush: bool = True, told: list[float] | None = None
) -> BargeIn:
    """The barge-in rules over ``playback``; ``told`` notes when an interjection was confirmed."""
    noted = told if told is not None else []
    return BargeIn(
        playback,
        call_id="call",
        flush=flush,
        interjection=lambda: noted.append(clock.now),
        clock=clock,
    )


def suspected(
    clock: FakeClock, sink: Sink, *, flush: bool = True, told: list[float] | None = None
) -> tuple[Playback, BargeIn]:
    """The assistant speaking (its audio runs until 1000.4), and speech begun over it at 1000.0."""
    playback = Playback(sink.frame, sink.flush, clock=clock)
    barge_in = barge_in_for(playback, clock, flush=flush, told=told)
    playback.add(pcm_ms(400))
    assert barge_in.person_began(4000.0)
    return playback, barge_in


def test_the_model_has_a_second_to_go_quiet_for_half_a_second() -> None:
    assert (CONFIRM_WINDOW_SECONDS, YIELD_SILENCE_SECONDS) == (1.0, 0.5)


async def test_a_model_that_talks_on_through_the_window_was_not_interrupted() -> None:
    clock, sink = FakeClock(), Sink()
    told: list[float] = []
    playback, barge_in = suspected(clock, sink, told=told)
    for _ in range(12):  # 100 ms of voice every 100 ms, as the service streams it
        clock.advance(0.1)
        playback.add(pcm_ms(100))
        assert await barge_in.settle() is False
    assert sink.flushes == 0
    assert (playback.interruptions, playback.discarded_bytes) == (0, 0)
    assert (barge_in.false_interruptions, told) == (1, [])
    assert playback.pacer.held_bytes == 1600 * 48  # every word of it kept for the browser


async def test_a_model_that_goes_quiet_for_the_person_is_cut_off_once() -> None:
    clock, sink = FakeClock(), Sink()
    told: list[float] = []
    playback, barge_in = suspected(clock, sink, told=told)
    clock.advance(0.2)
    playback.add(pcm_ms(200))  # it talks on while it takes them in
    assert await barge_in.settle() is False
    for _ in range(4):  # then stops: 100 ms of its quiet every 100 ms
        clock.advance(0.1)
        playback.add(noise_floor_ms(100))
        assert await barge_in.settle() is False
    # Nothing was flushed or thrown away while it was only suspected.
    assert (sink.flushes, playback.discarded_bytes) == (0, 0)
    clock.advance(0.1)
    playback.add(noise_floor_ms(100))  # half a second of it
    assert await barge_in.settle() is True
    assert (sink.flushes, playback.interruptions, told) == (1, 1, [clock.now])
    # All it still held, as an interruption drops, but for the quiet that said it stopped.
    assert playback.discarded_bytes == 600 * 48
    for _ in range(10):
        clock.advance(0.1)
        playback.add(noise_floor_ms(100))
        assert await barge_in.settle() is False
    assert sink.flushes == 1
    assert barge_in.false_interruptions == 0


@pytest.mark.parametrize(
    ("stops_at", "confirmed"),
    [(0.4, True), (CONFIRM_WINDOW_SECONDS, True), (CONFIRM_WINDOW_SECONDS + 0.01, False)],
)
async def test_a_model_that_goes_quiet_inside_the_window_is_confirmed_half_a_second_later(
    stops_at: float, confirmed: bool
) -> None:
    clock, sink = FakeClock(), Sink()
    playback, barge_in = suspected(clock, sink)
    # It talks on until `stops_at` after the speech start, and then sends nothing at all.
    playback.add(pcm_ms(round((stops_at - 0.4) * 1000)))
    clock.now = 1000.0 + stops_at + YIELD_SILENCE_SECONDS - 0.01
    assert await barge_in.settle() is False
    clock.now = 1000.0 + stops_at + YIELD_SILENCE_SECONDS
    assert await barge_in.settle() is confirmed
    assert (sink.flushes, playback.interruptions) == (int(confirmed), int(confirmed))
    assert barge_in.false_interruptions == int(not confirmed)


@pytest.mark.parametrize("quiet", [noise_floor_ms, lambda ms: pcm_ms(ms, level=0)])
@pytest.mark.parametrize(("quiet_ms", "confirmed"), [(400, False), (480, False), (500, True)])
async def test_half_a_second_of_quiet_says_the_model_stopped_and_a_pause_does_not(
    quiet: Callable[[int], bytes], quiet_ms: int, confirmed: bool
) -> None:
    clock, sink = FakeClock(), Sink()
    playback, barge_in = suspected(clock, sink)
    clock.advance(0.1)
    playback.add(quiet(quiet_ms))
    assert await barge_in.settle() is confirmed
    # After a pause between its sentences it talks on, through the window.
    clock.advance(0.1)
    playback.add(pcm_ms(100))
    clock.advance(1.0)
    playback.add(pcm_ms(100))
    assert await barge_in.settle() is False
    assert sink.flushes == int(confirmed)
    assert barge_in.false_interruptions == int(not confirmed)


async def test_a_speech_start_after_the_model_had_stopped_cuts_off_nothing() -> None:
    clock, sink = FakeClock(), Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    barge_in = barge_in_for(playback, clock)
    playback.add(pcm_ms(400) + noise_floor_ms(500))  # its reply, and the quiet after it
    assert playback.audible()  # its last words are still to be heard
    # Decided at once: there is nothing left for it to stop, and the browser plays its last words.
    assert barge_in.person_began(4000.0)
    assert barge_in.false_interruptions == 1
    assert await barge_in.settle() is False
    assert (sink.flushes, playback.discarded_bytes) == (0, 0)


async def test_audio_sent_ahead_of_time_is_no_gap_however_long_after_it_came() -> None:
    clock, sink = FakeClock(), Sink()
    playback, barge_in = suspected(clock, sink)
    playback.add(pcm_ms(1500))  # sent at once: its audio runs until 1001.9
    clock.now = 1001.0
    assert await barge_in.settle() is False
    assert barge_in.false_interruptions == 0


async def test_a_speech_start_after_the_stream_had_stopped_cuts_off_nothing() -> None:
    clock, sink = FakeClock(), Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    barge_in = barge_in_for(playback, clock)
    playback.add(pcm_ms(400))  # its reply, and then nothing at all
    clock.advance(0.4 + YIELD_SILENCE_SECONDS)
    assert playback.audible()  # its last words are still to be heard
    assert barge_in.person_began(4000.0)
    assert await barge_in.settle() is False
    assert (sink.flushes, barge_in.false_interruptions) == (0, 1)


async def test_speech_while_the_assistant_is_not_audible_is_not_suspected() -> None:
    clock, sink = FakeClock(), Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    barge_in = barge_in_for(playback, clock)
    playback.add(noise_floor_ms(500))  # quiet only
    assert barge_in.person_began(4000.0) is False
    playback.add(noise_floor_ms(500))
    assert await barge_in.settle() is False
    assert (sink.flushes, barge_in.false_interruptions) == (0, 0)


async def test_with_barge_in_off_a_confirmed_interruption_cuts_off_nothing() -> None:
    clock, sink = FakeClock(), Sink()
    told: list[float] = []
    playback, barge_in = suspected(clock, sink, flush=False, told=told)
    playback.add(noise_floor_ms(500))
    # Still confirmed, and the read-back gate is told, but nothing is flushed or discarded.
    assert await barge_in.settle() is True
    assert told == [clock.now]
    assert (sink.flushes, playback.interruptions, playback.discarded_bytes) == (0, 0, 0)


async def test_a_new_speech_start_before_the_last_is_decided_gets_a_window_of_its_own() -> None:
    clock, sink = FakeClock(), Sink()
    told: list[float] = []
    playback, barge_in = suspected(clock, sink, told=told)
    playback.add(pcm_ms(1000))  # it talks on: its audio runs until 1001.4
    clock.now = 1000.8
    assert await barge_in.settle() is False
    assert barge_in.person_began(4000.0)  # the person, this time
    assert barge_in.false_interruptions == 1  # the first is given up as false
    # It stops for them at 1001.4, past the first speech start's window, inside the second's.
    clock.now = 1001.4 + YIELD_SILENCE_SECONDS
    assert await barge_in.settle() is True
    assert (sink.flushes, told) == (1, [clock.now])
    assert barge_in.false_interruptions == 1


async def test_a_wait_for_the_verdict_ends_with_it_and_after_the_gate_was_told() -> None:
    clock, sink = FakeClock(), Sink()
    told: list[float] = []
    playback, barge_in = suspected(clock, sink, told=told)
    seen: list[list[float]] = []

    async def wait() -> None:
        await barge_in.settled()
        seen.append(list(told))

    waiting = asyncio.create_task(wait())
    await asyncio.sleep(0.01)
    assert not waiting.done()
    # A second speech start takes the first one's place: still undecided.
    clock.advance(0.3)
    playback.add(pcm_ms(100))
    assert barge_in.person_began(4000.0)
    await asyncio.sleep(0.01)
    assert not waiting.done()
    playback.add(noise_floor_ms(500))
    assert await barge_in.settle() is True
    await asyncio.wait_for(waiting, 1)
    assert seen == [[clock.now]]


async def test_a_wait_for_the_verdict_on_speech_the_model_talks_through_tells_no_one() -> None:
    clock, sink = FakeClock(), Sink()
    told: list[float] = []
    playback, barge_in = suspected(clock, sink, told=told)
    waiting = asyncio.create_task(barge_in.settled())
    playback.add(pcm_ms(1200))  # it talks on: its audio runs until 1001.6
    clock.now = 1001.2
    playback.add(pcm_ms(100))
    assert await barge_in.settle() is False
    await asyncio.wait_for(waiting, 1)
    assert told == []
    await asyncio.wait_for(barge_in.settled(), 1)  # nothing waits to be decided: no wait at all


# ---- a speech start over the assistant, and the discard windows ----


async def test_once_confirmed_the_discard_runs_as_before_from_the_confirmation() -> None:
    clock, sink = FakeClock(), Sink()
    playback, barge_in = suspected(clock, sink)
    clock.advance(0.6)
    playback.add(noise_floor_ms(500))
    assert await barge_in.settle() is True  # 0.6 s after the speech start
    confirmed_at = clock.now
    # The person talks on: their speech keeps the model's audio discarded, for as long as the cap
    # allows from the confirmation (which is past the cap from the speech start).
    while clock.now < confirmed_at + SUPPRESS_CAP_SECONDS - 0.1:
        clock.advance(0.1)
        playback.person_speaking()
        playback.add(pcm_ms(100))
        assert not playback.audible()
    # And a new speech start meanwhile is not suspected: nothing is audible to interrupt.
    assert barge_in.person_began(4000.0) is False
    clock.advance(SUPPRESS_HOLD_SECONDS + 0.2)
    playback.add(pcm_ms(100))
    assert playback.audible()
    assert sink.flushes == 1


async def test_a_new_speech_start_soon_after_an_interruption_discards_nothing_until_confirmed() -> (
    None
):
    clock, sink = FakeClock(), Sink()
    playback, barge_in = suspected(clock, sink)
    playback.add(noise_floor_ms(500))
    assert await barge_in.settle() is True
    discarded = playback.discarded_bytes
    # The person stops, the discard lapses, and the model answers them...
    clock.advance(SUPPRESS_HOLD_SECONDS + 0.1)
    playback.add(pcm_ms(400))
    # ...and well inside the first interruption's cap, its own voice starts the detector's speech
    # again, which goes on for every frame the call reports.
    assert barge_in.person_began(4000.0)
    for _ in range(5):
        playback.person_speaking()
        clock.advance(0.1)
        playback.add(pcm_ms(100))
        assert await barge_in.settle() is False
    # What the model says meanwhile is taken in, not thrown away on the old interruption's account.
    assert playback.discarded_bytes == discarded
    assert playback.pacer.held_bytes == 900 * 48
    assert sink.flushes == 1


def test_clearing_cuts_the_stream_off_where_its_audio_is_dropped() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(5000), 0.0)  # far ahead of real time: its audio would run until 5.0
    pacer.clear(1.0)
    # Nothing came after the audio that was dropped at 1.0: the model has been quiet since.
    assert pacer.quiet_bytes(1.6) == 600 * 48


# ---- what each speech start over the assistant leaves in the log ----


def barge_in_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [
        r.fields  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "live barge-in"
    ]


async def test_a_false_interruption_is_written_down_when_it_is_decided(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.barge_in")
    clock, sink = FakeClock(), Sink()
    clock.advance(0.25)
    playback, barge_in = suspected(clock, sink)
    playback.add(pcm_ms(1500))  # it talks on: its audio runs until 1002.15
    clock.advance(1.5)
    playback.add(pcm_ms(100))
    await barge_in.settle()
    assert barge_in_lines(caplog) == [
        {
            "call_id": "call",
            "confirmed": False,
            "segment_ms": 0,
            "start_level": 4000,
            "wait_ms": 1500,
            "voice_ms": 1600,
            "discarded_ms": 0,
        }
    ]


async def test_an_interruption_is_written_down_with_all_it_discarded_once_that_is_over(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.barge_in")
    clock, sink = FakeClock(), Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    barge_in = barge_in_for(playback, clock)
    playback.add(pcm_ms(400))
    clock.advance(0.25)
    playback.add(pcm_ms(300))
    assert barge_in.person_began(1234.4)  # 0.25 s into what the assistant says
    clock.advance(0.25)
    playback.add(pcm_ms(100))
    playback.add(noise_floor_ms(500))
    assert await barge_in.settle() is True
    playback.add(pcm_ms(200))  # what it says while the person talks: thrown away
    assert await barge_in.settle() is False
    assert barge_in_lines(caplog) == []  # the discard is still running
    clock.advance(SUPPRESS_HOLD_SECONDS)
    await barge_in.settle()
    # The 800 ms of voice it still held and the 200 ms after; not the quiet that confirmed it.
    assert barge_in_lines(caplog) == [
        {
            "call_id": "call",
            "confirmed": True,
            "segment_ms": 250,
            "start_level": 1234,
            "wait_ms": 250,
            "voice_ms": 100,
            "discarded_ms": 1000,
        }
    ]


async def test_what_is_undecided_when_the_call_ends_is_written_down_then(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.barge_in")
    clock, sink = FakeClock(), Sink()
    _, pending = suspected(clock, sink)
    clock.advance(0.3)
    pending.close()
    assert pending.false_interruptions == 1
    playback, cutting = suspected(clock, sink)
    playback.add(noise_floor_ms(500))
    assert await cutting.settle() is True
    cutting.close()
    lines = barge_in_lines(caplog)
    assert [(line["confirmed"], line["wait_ms"], line["discarded_ms"]) for line in lines] == [
        (False, 300, 0),
        (True, 0, 400),
    ]


# ---- voice and silence ----


def test_the_pacer_calls_voice_audible_while_it_is_held_and_for_a_moment_after() -> None:
    pacer = OutputPacer(playout_delay=0.0, classify=lambda frame, now: frame != bytes(len(frame)))
    assert not pacer.audible(0.0)
    assert pacer.add(pcm_ms(200), 0.0) is True
    assert pacer.audible(0.0)
    # Sent: 0.2 s of voice is heard until 0.2 s from now, and a pause of half a second more.
    assert sum(map(len, pacer.due(0.0))) == 200 * 48
    assert pacer.audible(0.19)
    assert pacer.audible(0.6)
    assert not pacer.audible(0.71)


def test_silence_is_paced_and_sent_but_is_never_audible() -> None:
    pacer = OutputPacer(classify=lambda frame, now: frame != bytes(len(frame)))
    silence = pcm_ms(300, level=0)
    assert pacer.add(silence, 0.0) is False
    assert not pacer.audible(0.0)
    assert b"".join(pacer.due(0.0)) == silence
    assert not pacer.audible(0.1)
    assert pacer.held_bytes == 0


def test_voice_in_the_middle_of_silence_is_found_frame_by_frame() -> None:
    pacer = OutputPacer(playout_delay=0.0, classify=lambda frame, now: frame != bytes(len(frame)))
    mixed = pcm_ms(400, level=0) + pcm_ms(80) + pcm_ms(400, level=0)
    assert pacer.add(mixed, 0.0) is True
    assert pacer.audible(0.0)
    for moment in (0.0, 0.4, 0.8, 1.2):
        pacer.due(moment)
    # Only the 80 ms of voice counts: it is heard from 0.4 to 0.48 s, plus the pause.
    assert pacer.audible(0.9)
    assert not pacer.audible(1.0)


def test_clearing_forgets_the_voice_too() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(1000), 0.0)
    pacer.due(0.0)
    pacer.clear(0.1)
    assert not pacer.audible(0.1)


async def test_a_stream_of_silence_is_not_the_assistant_speaking() -> None:
    sink = Sink()
    clock = FakeClock()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    assert playback.add(pcm_ms(200, level=0)) is False
    assert not playback.audible()
    assert playback.add(pcm_ms(200)) is True
    assert playback.audible()


async def test_silence_after_voice_ends_the_speaking_after_the_pause() -> None:
    sink = Sink()
    clock = FakeClock()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    playback.set_playout_delay(0.0)
    task = asyncio.create_task(playback.run())
    playback.add(pcm_ms(200))
    await until(lambda: sum(map(len, sink.frames)) == 200 * 48)
    assert playback.audible()
    clock.advance(0.3)
    assert playback.audible()
    clock.advance(0.5)
    assert not playback.audible()
    task.cancel()


# ---- when the model's audio is late ----


def test_audio_arriving_after_the_queue_ran_dry_mid_reply_is_one_starvation() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(1000), 10.0)
    pacer.due(10.0)  # all of it is sent: the browser plays until 11.0
    pacer.add(pcm_ms(100), 11.4)  # it ran dry 0.4 s ago
    assert pacer.starved == 1
    assert pacer.starved_seconds == pytest.approx(0.4)


def test_audio_arriving_while_the_browser_still_has_some_to_play_is_not_starvation() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(1000), 10.0)
    pacer.due(10.0)
    pacer.add(pcm_ms(100), 10.9)
    pacer.add(pcm_ms(100), 11.0)  # exactly as it runs dry is not a gap yet
    assert pacer.starved == 0


def test_audio_arriving_while_the_pacer_still_holds_some_is_not_starvation() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(4000), 10.0)
    pacer.due(10.0)  # the lead goes out, the rest is held
    pacer.add(pcm_ms(100), 11.7)
    assert pacer.starved == 0


@pytest.mark.parametrize(
    ("silence", "starved"),
    [
        (0.001, 0),
        (0.02, 0),
        (STARVATION_MIN_SECONDS - 0.01, 0),
        (STARVATION_MIN_SECONDS + 0.01, 1),
        (0.5, 1),
        (STARVATION_WINDOW_SECONDS - 0.01, 1),
        (STARVATION_WINDOW_SECONDS + 0.01, 0),
        (30.0, 0),
    ],
)
def test_a_gap_is_starvation_from_fifty_milliseconds_to_two_seconds_and_no_other(
    silence: float, starved: int
) -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(1000), 10.0)
    pacer.due(10.0)
    pacer.add(pcm_ms(100), 11.0 + silence)
    assert pacer.starved == starved
    assert pacer.starved_seconds == pytest.approx(silence if starved else 0.0)


def test_jitter_on_a_stream_at_real_time_is_not_starvation_but_a_stall_in_it_is() -> None:
    pacer = OutputPacer()
    now = 10.0
    pacer.add(pcm_ms(100), now)
    pacer.due(now)
    # Each chunk of 100 ms arrives a little after the browser has played the one before.
    for lateness in (0.002, 0.015, 0.03, 0.049, 0.001, 0.02):
        now += 0.1 + lateness
        pacer.add(pcm_ms(100), now)
        pacer.due(now)
    assert (pacer.starved, pacer.starved_seconds) == (0, 0.0)
    now += 0.1 + 0.3
    pacer.add(pcm_ms(100), now)
    assert pacer.starved == 1
    assert pacer.starved_seconds == pytest.approx(0.3)


def test_the_longest_gap_is_kept_beside_the_count_and_the_total() -> None:
    pacer = OutputPacer()
    now = 10.0
    pacer.add(pcm_ms(100), now)
    pacer.due(now)
    for gap in (0.1, 0.6, 0.3):
        now += 0.1 + gap
        pacer.add(pcm_ms(100), now)
        pacer.due(now)
    assert pacer.starved == 3
    assert pacer.starved_seconds == pytest.approx(1.0)
    assert pacer.starved_max_seconds == pytest.approx(0.6)


def test_the_first_audio_of_a_call_and_the_audio_after_an_interruption_are_not_starvation() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(100), 0.5)
    assert pacer.starved == 0

    pacer.due(0.5)
    pacer.add(pcm_ms(1000), 10.0)
    pacer.due(10.0)
    # The person cut the assistant off: the empty queue that follows is their doing, however soon
    # the model speaks again.
    pacer.clear(10.5)
    pacer.add(pcm_ms(100), 11.2)
    assert pacer.starved == 0

    # But once something has been sent again, a late chunk counts as usual.
    pacer.due(11.2)
    pacer.add(pcm_ms(100), 12.0)
    assert pacer.starved == 1
    assert pacer.starved_seconds == pytest.approx(0.7)


def test_every_late_chunk_adds_to_the_count_and_the_silence() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(200), 10.0)
    pacer.due(10.0)
    now = 10.2
    for silence in (0.3, 0.5, 0.2):
        now += silence
        pacer.add(pcm_ms(200), now)
        pacer.due(now)
        now += 0.2
    assert pacer.starved == 3
    assert pacer.starved_seconds == pytest.approx(1.0)


def test_an_empty_chunk_neither_counts_nor_hides_the_silence_before_the_next_one() -> None:
    pacer = OutputPacer()
    pacer.add(pcm_ms(1000), 10.0)
    pacer.due(10.0)
    assert pacer.add(b"", 11.4) is False
    assert pacer.due(11.4) == []
    assert pacer.starved == 0
    pacer.add(pcm_ms(100), 11.5)
    assert pacer.starved_seconds == pytest.approx(0.5)


async def test_what_pacing_cost_is_reported_in_milliseconds() -> None:
    clock = FakeClock()
    sink = Sink()
    playback = Playback(
        sink.frame, sink.flush, clock=clock, pacer=OutputPacer(max_held_seconds=1.0)
    )
    assert playback.record() == {
        "discarded_audio_ms": 0,
        "dropped_audio_ms": 0,
        "starved": 0,
        "starved_ms": 0,
        "starved_max_ms": None,
    }
    # Three seconds at once with room for one: two seconds are dropped from the front.
    playback.add(pcm_ms(3000))
    assert playback.record()["dropped_audio_ms"] == 2000
    # An interruption discards the second still held, and the half second the model adds after it.
    await playback.interrupt()
    playback.add(pcm_ms(500))
    assert playback.record()["discarded_audio_ms"] == 1500

    task = asyncio.create_task(playback.run())
    clock.advance(SUPPRESS_CAP_SECONDS + 1)
    playback.add(pcm_ms(500))
    await until(lambda: sum(map(len, sink.frames)) == 500 * 48)
    clock.advance(1.0)  # played out half a second ago
    playback.add(pcm_ms(100))
    await until(lambda: sum(map(len, sink.frames)) == 600 * 48)
    task.cancel()
    assert playback.record() == {
        "discarded_audio_ms": 1500,
        "dropped_audio_ms": 2000,
        "starved": 1,
        "starved_ms": 500,
        "starved_max_ms": 500,
    }


# ---- the browser's playout delay ----


def test_a_playout_delay_of_four_tenths_of_a_second_is_assumed_until_the_browser_reports_one() -> (
    None
):
    assert PLAYOUT_DELAY_SECONDS == 0.4
    assert OutputPacer().playout_delay == PLAYOUT_DELAY_SECONDS


@pytest.mark.parametrize("delay", [0.0, 0.4, 1.0, 2.0])
def test_the_voice_is_heard_until_its_end_plus_the_playout_delay_and_the_pause(
    delay: float,
) -> None:
    pacer = OutputPacer(playout_delay=delay)
    pacer.add(pcm_ms(200), 10.0)
    assert sum(map(len, pacer.due(10.0))) == 200 * 48  # sent at once: the playhead ends at 10.2
    heard_until = 10.2 + delay + VOICE_HANGOVER_SECONDS
    assert pacer.audible(heard_until - 0.02)
    assert not pacer.audible(heard_until + 0.02)


def test_the_delay_counts_for_speaking_whatever_the_grace() -> None:
    pacer = OutputPacer(playout_delay=1.0)
    pacer.add(pcm_ms(200), 10.0)
    pacer.due(10.0)
    # The browser plays the voice from 11.0 to 11.2.
    assert pacer.speaking(11.1, 0.0)
    assert not pacer.speaking(11.3, 0.0)
    assert pacer.speaking(11.6, 0.5)
    assert not pacer.speaking(11.8, 0.5)


@pytest.mark.parametrize("delay", [0.0, 2.0])
def test_voice_that_is_still_held_is_audible_whatever_the_delay(delay: float) -> None:
    pacer = OutputPacer(playout_delay=delay)
    pacer.add(pcm_ms(200), 0.0)
    assert pacer.audible(500.0)


def test_silence_is_never_audible_however_long_the_delay() -> None:
    pacer = OutputPacer(playout_delay=2.0, classify=lambda frame, now: frame != bytes(len(frame)))
    pacer.add(pcm_ms(300, level=0), 0.0)
    pacer.due(0.0)
    assert not pacer.audible(0.1)


async def test_a_delay_the_browser_reports_replaces_the_assumed_one() -> None:
    sink = Sink()
    clock = FakeClock()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    task = asyncio.create_task(playback.run())
    playback.add(pcm_ms(200))
    await until(lambda: sum(map(len, sink.frames)) == 200 * 48)
    # 0.2 s of voice, the assumed 0.4 s of delay and the half second's pause: heard until 1.1 s.
    clock.advance(1.0)
    assert playback.audible()
    clock.advance(0.2)
    assert not playback.audible()
    # The browser says it holds a frame for a whole second: heard until 1.7 s.
    playback.set_playout_delay(1.0)
    assert playback.audible()
    clock.advance(0.6)
    assert not playback.audible()
    task.cancel()


def test_the_delay_does_not_change_what_starvation_measures() -> None:
    for delay in (0.0, 1.0):
        pacer = OutputPacer(playout_delay=delay)
        pacer.add(pcm_ms(1000), 10.0)
        pacer.due(10.0)
        pacer.add(pcm_ms(100), 11.4)
        assert pacer.starved == 1
        assert pacer.starved_seconds == pytest.approx(0.4)


# ---- how much of what was taken in was voice ----


def test_the_pacer_counts_the_voice_it_took_in_and_not_the_silence() -> None:
    pacer = OutputPacer(classify=lambda frame, now: frame != bytes(len(frame)))
    mixed = pcm_ms(400, level=0) + pcm_ms(80) + pcm_ms(400, level=0)
    pacer.add(mixed, 0.0)
    assert pacer.voiced_bytes == 80 * 48
    pacer.add(pcm_ms(1000, level=0), 1.0)
    assert pacer.voiced_bytes == 80 * 48
    pacer.add(pcm_ms(120), 2.0)
    assert pacer.voiced_bytes == 200 * 48


async def test_audio_discarded_because_the_person_talks_is_not_voice_taken_in() -> None:
    clock = FakeClock()
    sink = Sink()
    playback = Playback(sink.frame, sink.flush, clock=clock)
    playback.add(pcm_ms(1000))
    assert playback.voiced_seconds == pytest.approx(1.0)
    await playback.interrupt()
    playback.add(pcm_ms(500))  # said while the person is talking: thrown away
    assert playback.voiced_seconds == pytest.approx(1.0)
    clock.advance(SUPPRESS_CAP_SECONDS + 1)
    playback.add(pcm_ms(500, level=0))  # silence, once it may be heard again
    assert playback.voiced_seconds == pytest.approx(1.0)
    playback.add(pcm_ms(500))
    assert playback.voiced_seconds == pytest.approx(1.5)
