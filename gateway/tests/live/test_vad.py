"""The energy detector: speech begins after a short run of loud audio, ends after a hangover."""

from __future__ import annotations

import struct

import pytest

from rc_gateway.live.vad import (
    ABSOLUTE_THRESHOLD,
    HANGOVER_SECONDS,
    ONSET_MS,
    OUTPUT_THRESHOLD,
    EnergyVad,
    VoiceDetector,
)

from .fakes import LOUD, QUIET

FRAME_SECONDS = 0.02


def frames(vad: EnergyVad, frame: bytes, count: int, start: float) -> list[tuple[bool, bool]]:
    out = []
    for index in range(count):
        result = vad.process(frame, start + index * FRAME_SECONDS)
        out.append((result.speaking, result.onset))
    return out


def test_silence_is_not_speech() -> None:
    vad = EnergyVad()
    assert not any(speaking for speaking, _ in frames(vad, QUIET, 100, 0.0))


def test_speech_begins_after_the_onset_run_and_reports_it_once() -> None:
    vad = EnergyVad()
    seen = frames(vad, LOUD, 20, 0.0)
    first = next(index for index, (speaking, _) in enumerate(seen) if speaking)
    assert (first + 1) * FRAME_SECONDS * 1000 >= ONSET_MS
    assert (first) * FRAME_SECONDS * 1000 < ONSET_MS
    assert [onset for _, onset in seen].count(True) == 1
    assert all(speaking for speaking, _ in seen[first:])


def test_a_click_is_not_speech() -> None:
    vad = EnergyVad()
    seen = frames(vad, LOUD, 2, 0.0) + frames(vad, QUIET, 10, 0.04)
    assert not any(speaking for speaking, _ in seen)


def test_speech_outlasts_its_last_loud_frame_by_the_hangover_only() -> None:
    vad = EnergyVad()
    frames(vad, LOUD, 10, 0.0)
    last_loud = 9 * FRAME_SECONDS
    assert vad.process(QUIET, last_loud + HANGOVER_SECONDS * 0.5).speaking
    assert not vad.process(QUIET, last_loud + HANGOVER_SECONDS * 1.01).speaking


def test_the_end_of_speech_is_reported_once_with_the_time_of_its_last_loud_frame() -> None:
    vad = EnergyVad()
    frames(vad, LOUD, 10, 0.0)
    last_loud = 9 * FRAME_SECONDS
    results = [vad.process(QUIET, last_loud + FRAME_SECONDS * step) for step in range(1, 40)]
    ends = [result for result in results if result.ended_at is not None]
    assert [result.ended_at for result in ends] == [pytest.approx(last_loud)]
    # It is found on the first quiet frame at or after the hangover, and not before.
    assert ends[0] is next(result for result in results if not result.speaking)
    assert not any(result.speaking for result in results[results.index(ends[0]) :])
    assert all(result.ended_at is None for result in results if result is not ends[0])


def test_a_click_that_never_became_speech_has_no_end() -> None:
    vad = EnergyVad()
    vad.process(LOUD, 0.0)
    vad.process(LOUD, 0.02)
    results = [vad.process(QUIET, 0.04 + index * FRAME_SECONDS) for index in range(30)]
    assert all(result.ended_at is None for result in results)


def test_each_utterance_ends_on_its_own() -> None:
    vad = EnergyVad()
    ended: list[float] = []
    now = 0.0
    for _ in range(3):
        for frame, count in ((LOUD, 12), (QUIET, 30)):
            for _ in range(count):
                result = vad.process(frame, now)
                if result.ended_at is not None:
                    ended.append(result.ended_at)
                now += FRAME_SECONDS
    assert len(ended) == 3
    assert ended == sorted(ended)


def test_a_new_utterance_reports_a_new_onset() -> None:
    vad = EnergyVad()
    frames(vad, LOUD, 10, 0.0)
    frames(vad, QUIET, 40, 0.2)
    again = frames(vad, LOUD, 10, 2.0)
    assert [onset for _, onset in again].count(True) == 1


def test_a_hum_under_the_floor_never_triggers() -> None:
    vad = EnergyVad()
    hum = struct.pack("<h", int(ABSOLUTE_THRESHOLD * 0.9)) * 480
    assert not any(speaking for speaking, _ in frames(vad, hum, 200, 0.0))


def test_a_loud_room_needs_speech_well_above_it() -> None:
    vad = EnergyVad()
    # The room sits at 1500: far above a quiet room, and never speech however long it lasts.
    room = struct.pack("<h", 1500) * 480
    seen = frames(vad, room, 300, 0.0)
    # Heard as speech for the seconds it takes to learn the room, then never again.
    assert not any(speaking for speaking, _ in seen[-100:])
    assert vad.noise == 1500.0
    louder = struct.pack("<h", 6500) * 480
    assert any(speaking for speaking, _ in frames(vad, louder, 20, 6.4))


def test_speech_with_pauses_never_becomes_the_floor() -> None:
    vad = EnergyVad()
    quiet = struct.pack("<h", 150) * 480
    now = 0.0
    onsets = 0
    for _ in range(25):
        for frame, count in ((LOUD, 12), (quiet, 20)):
            for _ in range(count):
                result = vad.process(frame, now)
                onsets += result.onset
                now += FRAME_SECONDS
    assert vad.noise == 150.0
    assert onsets == 25


def test_the_floor_forgets_a_noise_that_has_stopped() -> None:
    vad = EnergyVad()
    room = struct.pack("<h", 1500) * 480
    frames(vad, room, 200, 0.0)
    assert vad.noise == 1500.0
    frames(vad, QUIET, 200, 4.0)
    assert vad.noise == 10.0


def loud_frames(detector: VoiceDetector, frame: bytes, count: int, start: float) -> list[bool]:
    return [detector.loud(frame, start + index * FRAME_SECONDS) for index in range(count)]


def test_the_assistants_silence_is_not_voice_and_its_speech_is() -> None:
    detector = VoiceDetector()
    assert not any(loud_frames(detector, QUIET, 50, 0.0))
    assert all(loud_frames(detector, LOUD, 5, 1.0))
    assert not any(loud_frames(detector, QUIET, 5, 2.0))


def test_the_assistants_speech_is_heard_from_its_very_first_frame() -> None:
    assert VoiceDetector().loud(LOUD, 0.0)


def test_quiet_synthesized_speech_counts_but_a_faint_noise_floor_does_not() -> None:
    detector = VoiceDetector()
    faint = struct.pack("<h", int(OUTPUT_THRESHOLD * 0.8)) * 480
    quiet_speech = struct.pack("<h", int(OUTPUT_THRESHOLD * 4)) * 480
    assert not any(loud_frames(detector, faint, 100, 0.0))
    assert all(loud_frames(detector, quiet_speech, 5, 2.0))


def test_a_noise_floor_the_model_always_streams_is_learned() -> None:
    detector = VoiceDetector()
    floor = struct.pack("<h", 250) * 480
    loud_frames(detector, floor, 200, 0.0)
    assert not any(loud_frames(detector, floor, 50, 4.0))
    assert all(loud_frames(detector, struct.pack("<h", 4000) * 480, 3, 5.0))


def test_a_long_stretch_of_steady_speech_does_not_become_the_floor() -> None:
    detector = VoiceDetector()
    speech = struct.pack("<h", 1000) * 480
    # Ten seconds of it with nothing quieter: still voice at the end.
    assert all(loud_frames(detector, speech, 500, 0.0))
