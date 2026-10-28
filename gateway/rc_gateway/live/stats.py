"""Timing numbers for a call's usage line: durations, kept as small bounded series.

Only numbers are kept, never anything said. They answer one question after a call that felt slow:
where did the time go. This module holds the series, the timer for how long the assistant takes to
start answering and the tally of what was heard from the person; the backend's and the tools'
timings live with what they time (``work.py``).
"""

from __future__ import annotations

import statistics

from .protocol import BYTES_PER_SECOND

#: The most values one series keeps. A call is bounded in time, so this is never met in practice.
MAX_SAMPLES = 512
#: A voice that starts later than this after the person stopped is not an answer to them.
REPLY_WINDOW_SECONDS = 30.0


class Samples:
    """Durations in seconds: how many, and the median and the largest in whole milliseconds."""

    def __init__(self) -> None:
        self._values: list[float] = []

    def add(self, seconds: float) -> None:
        if len(self._values) < MAX_SAMPLES:
            self._values.append(max(0.0, seconds))

    @property
    def count(self) -> int:
        return len(self._values)

    def median_ms(self) -> int | None:
        return round(statistics.median(self._values) * 1000) if self._values else None

    def max_ms(self) -> int | None:
        return round(max(self._values) * 1000) if self._values else None


class ReplyTimer:
    """How long the assistant takes to start speaking once the person has stopped.

    Timed from the person's last loud microphone frame (when the detector ends their speech) to the
    first voiced frame of the assistant's audio reaching the gateway. Nothing is timed when the
    assistant was already audible as the person stopped, when the person speaks again before it
    answers, or when its voice starts more than ``REPLY_WINDOW_SECONDS`` later.
    """

    def __init__(self) -> None:
        self._stopped_at: float | None = None
        self.samples = Samples()

    def person_began(self) -> None:
        """The person is speaking again: what they said before is no longer being answered."""
        self._stopped_at = None

    def person_stopped(self, stopped_at: float, *, assistant_audible: bool) -> None:
        self._stopped_at = None if assistant_audible else stopped_at

    def voice_arrived(self, now: float) -> None:
        """Voiced audio from the assistant reached the gateway. Only the first one counts."""
        stopped_at, self._stopped_at = self._stopped_at, None
        if stopped_at is not None and now - stopped_at <= REPLY_WINDOW_SECONDS:
            self.samples.add(now - stopped_at)

    def record(self) -> dict[str, int | None]:
        return {
            "replies": self.samples.count,
            "reply_ms_median": self.samples.median_ms(),
            "reply_ms_max": self.samples.max_ms(),
        }


class InputMeter:
    """What the call heard from the person, in numbers.

    How long they spoke by the detector's reckoning (from its onset to the end it finds, the
    hangover included) and how many fragments of transcript the service sent back. Together they
    tell a person who did not speak from a model that did not answer.
    """

    def __init__(self) -> None:
        self._speech_bytes = 0
        self._transcripts = 0

    def speech(self, frame_bytes: int) -> None:
        """A microphone frame that the detector held to be speech."""
        self._speech_bytes += frame_bytes

    def transcript(self) -> None:
        """A fragment of the person's transcript arrived."""
        self._transcripts += 1

    def record(self) -> dict[str, int]:
        return {
            "speech_ms": round(self._speech_bytes / BYTES_PER_SECOND * 1000),
            "input_transcripts": self._transcripts,
        }
