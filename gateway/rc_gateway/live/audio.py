"""Microphone frames from the browser: validated, and measured."""

from __future__ import annotations

import sys
import time
from array import array
from collections.abc import Callable

from .errors import LiveError
from .protocol import BYTES_PER_SECOND, MAX_MIC_FRAME_MS, MIN_MIC_FRAME_MS

#: Audio the browser may send ahead of real time, and how much faster than real time it may sustain.
BURST_SECONDS = 2.0
SPEED_LIMIT = 1.25

MIN_FRAME_BYTES = BYTES_PER_SECOND * MIN_MIC_FRAME_MS // 1000
MAX_FRAME_BYTES = BYTES_PER_SECOND * MAX_MIC_FRAME_MS // 1000


class MicFrameGate:
    """Refuse frames that are not microphone audio: odd, tiny, huge, or faster than real time.

    The rate is a token bucket measured in seconds of audio. A browser sending exactly in real
    time never drains it; a tab that was throttled and catches up may spend the burst; a client
    that floods runs it dry and is refused.
    """

    def __init__(
        self,
        *,
        burst_seconds: float = BURST_SECONDS,
        speed: float = SPEED_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst = burst_seconds
        self._speed = speed
        self._clock = clock
        self._tokens = burst_seconds
        self._last = clock()

    def check(self, frame: bytes) -> None:
        size = len(frame)
        if size % 2 or not MIN_FRAME_BYTES <= size <= MAX_FRAME_BYTES:
            raise LiveError("protocol_error", "an audio frame has an invalid size")
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._speed)
        self._last = now
        self._tokens -= size / BYTES_PER_SECOND
        if self._tokens < 0:
            raise LiveError("protocol_error", "audio arrives faster than real time")


def mean_abs(pcm: bytes) -> float:
    """The mean absolute amplitude of PCM16LE audio, in sample units (0 for an empty frame)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return sum(map(abs, samples)) / len(samples) if samples else 0.0
