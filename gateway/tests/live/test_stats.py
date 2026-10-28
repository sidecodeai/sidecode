"""The timing series, and the clock on how long the assistant takes to start answering."""

from __future__ import annotations

import pytest

from rc_gateway.live.stats import (
    MAX_SAMPLES,
    REPLY_WINDOW_SECONDS,
    InputMeter,
    ReplyTimer,
    Samples,
)


def test_a_series_reports_its_count_median_and_largest_in_whole_milliseconds() -> None:
    series = Samples()
    for seconds in (0.4, 2.0, 0.9):
        series.add(seconds)
    assert (series.count, series.median_ms(), series.max_ms()) == (3, 900, 2000)


def test_the_median_of_an_even_count_is_halfway_between_the_middle_two() -> None:
    series = Samples()
    for seconds in (1.0, 2.0, 3.0, 10.0):
        series.add(seconds)
    assert series.median_ms() == 2500


def test_a_series_with_nothing_in_it_has_nothing_to_report() -> None:
    series = Samples()
    assert (series.count, series.median_ms(), series.max_ms()) == (0, None, None)


def test_a_clock_that_ran_backwards_is_a_duration_of_zero() -> None:
    series = Samples()
    series.add(-3.0)
    assert (series.median_ms(), series.max_ms()) == (0, 0)


def test_a_series_is_bounded() -> None:
    series = Samples()
    for _ in range(MAX_SAMPLES + 100):
        series.add(1.0)
    assert series.count == MAX_SAMPLES


# ---- how long the assistant takes to start answering ----


def test_the_answer_is_timed_from_the_end_of_the_persons_speech_to_the_first_voice() -> None:
    timer = ReplyTimer()
    timer.person_stopped(10.0, assistant_audible=False)
    timer.voice_arrived(11.25)
    assert timer.record() == {"replies": 1, "reply_ms_median": 1250, "reply_ms_max": 1250}


def test_only_the_first_voice_after_the_person_stopped_counts() -> None:
    timer = ReplyTimer()
    timer.person_stopped(10.0, assistant_audible=False)
    timer.voice_arrived(11.0)
    timer.voice_arrived(14.0)
    assert timer.samples.count == 1


def test_a_voice_with_nobody_having_spoken_is_no_reply() -> None:
    timer = ReplyTimer()
    timer.voice_arrived(5.0)
    assert timer.samples.count == 0


def test_an_assistant_that_was_already_audible_when_the_person_stopped_is_not_timed() -> None:
    timer = ReplyTimer()
    timer.person_stopped(10.0, assistant_audible=True)
    timer.voice_arrived(11.0)
    assert timer.samples.count == 0


def test_a_later_stop_replaces_an_earlier_one_and_an_audible_one_cancels_it() -> None:
    timer = ReplyTimer()
    timer.person_stopped(10.0, assistant_audible=False)
    timer.person_stopped(12.0, assistant_audible=False)
    timer.voice_arrived(13.0)
    assert timer.record()["reply_ms_max"] == 1000

    timer.person_stopped(20.0, assistant_audible=False)
    timer.person_stopped(21.0, assistant_audible=True)
    timer.voice_arrived(22.0)
    assert timer.samples.count == 1


def test_the_person_speaking_again_before_an_answer_cancels_the_timing() -> None:
    timer = ReplyTimer()
    timer.person_stopped(10.0, assistant_audible=False)
    timer.person_began()
    timer.voice_arrived(11.0)
    assert timer.samples.count == 0


@pytest.mark.parametrize(
    ("late", "counted"), [(0.1, True), (REPLY_WINDOW_SECONDS, True), (31, False)]
)
def test_a_voice_more_than_the_window_after_the_person_stopped_is_not_an_answer(
    late: float, counted: bool
) -> None:
    timer = ReplyTimer()
    timer.person_stopped(100.0, assistant_audible=False)
    timer.voice_arrived(100.0 + late)
    assert (timer.samples.count == 1) is counted
    # Whether it counted or not, that voice has used up the timing.
    timer.voice_arrived(100.0 + late + 1)
    assert timer.samples.count == int(counted)


def test_replies_are_counted_with_their_median_and_largest() -> None:
    timer = ReplyTimer()
    for stopped, heard in ((10.0, 11.0), (20.0, 23.0), (30.0, 32.0)):
        timer.person_stopped(stopped, assistant_audible=False)
        timer.voice_arrived(heard)
    assert timer.record() == {"replies": 3, "reply_ms_median": 2000, "reply_ms_max": 3000}


def test_no_replies_are_null_not_zero() -> None:
    assert ReplyTimer().record() == {"replies": 0, "reply_ms_median": None, "reply_ms_max": None}


# ---- what was heard from the person ----


def test_speech_is_the_microphone_audio_the_detector_held_to_be_speech() -> None:
    meter = InputMeter()
    for _ in range(50):
        meter.speech(960)  # a frame of 20 ms
    meter.speech(4800)  # one of 100 ms
    assert meter.record()["speech_ms"] == 1100


def test_transcript_fragments_are_counted() -> None:
    meter = InputMeter()
    for _ in range(3):
        meter.transcript()
    assert meter.record()["input_transcripts"] == 3


def test_a_person_who_was_not_heard_is_zero_not_null() -> None:
    assert InputMeter().record() == {"speech_ms": 0, "input_transcripts": 0}
