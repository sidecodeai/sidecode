"""Small energy detectors for voice in audio: the person's, in the microphone frames, and the
assistant's, in the audio the Live API sends.

The Live API reports no "the user started speaking" event and takes no turn-detection setting
(see docs/LIVE_ASSISTANT.md), and its output may be a continuous stream that is mostly silence, so
the gateway listens for itself: it needs to know when to cut the assistant's queued speech, whether
the assistant is speaking at all, and when the call has gone quiet. Voice is audio louder than both
an absolute floor and a few times the room's own noise level, where the room's level is the quietest
of the last few seconds, so a steady hum is learned however loud it is, and speech, which has
pauses, never becomes the floor.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .audio import mean_abs
from .protocol import BYTES_PER_SECOND

#: Microphone: mean absolute amplitude (of 32768) under which nothing is speech, about -38 dBFS.
ABSOLUTE_THRESHOLD = 400.0
#: The assistant's audio is clean and synthesized, so its floor is lower, about -47 dBFS.
OUTPUT_THRESHOLD = 150.0
#: Its own noise floor is never taken to be above this, so a long stretch of steady speech cannot
#: teach the detector that speech is the floor.
OUTPUT_MAX_FLOOR = 2 * OUTPUT_THRESHOLD
#: How far above the room's level a frame must be.
NOISE_RATIO = 3.0
#: Loud audio this long, without a quiet frame, makes speech begin.
ONSET_MS = 100.0
#: Speech lasts this long after its last loud frame.
HANGOVER_SECONDS = 0.3
#: The room's level is the quietest of this much recent audio.
NOISE_WINDOW_SECONDS = 3.0
INITIAL_NOISE = 40.0
MIN_NOISE = 10.0


class NoiseFloor:
    """The quietest level of the last few seconds of audio."""

    def __init__(self, window_seconds: float = NOISE_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        #: (time, level) pairs whose levels rise from the front: the window's running minimum.
        self._quietest: deque[tuple[float, float]] = deque()

    @property
    def value(self) -> float:
        """The room's level, or a small default before any audio."""
        if not self._quietest:
            return INITIAL_NOISE
        return max(MIN_NOISE, self._quietest[0][1])

    def note(self, level: float, now: float) -> None:
        if not self._quietest:
            # Until real audio has shown how quiet the room is, assume a quiet one: speech that
            # starts with the audio is still heard, and a loud room is learned in seconds.
            self._quietest.append((now, INITIAL_NOISE))
        while self._quietest and self._quietest[-1][1] >= level:
            self._quietest.pop()
        self._quietest.append((now, level))
        while self._quietest[0][0] < now - self._window:
            self._quietest.popleft()


@dataclass(frozen=True)
class VadResult:
    speaking: bool
    #: True on the one frame at which speech began.
    onset: bool
    #: On the one frame at which speech was found to have ended: when its last loud frame was heard.
    ended_at: float | None = None


class EnergyVad:
    """Speech in the microphone: begins after a short run of loud audio, ends after a hangover."""

    def __init__(
        self,
        *,
        absolute_threshold: float = ABSOLUTE_THRESHOLD,
        noise_ratio: float = NOISE_RATIO,
        onset_ms: float = ONSET_MS,
        hangover_seconds: float = HANGOVER_SECONDS,
    ) -> None:
        self._absolute = absolute_threshold
        self._ratio = noise_ratio
        self._onset_ms = onset_ms
        self._hangover = hangover_seconds
        self._floor = NoiseFloor()
        self._loud_ms = 0.0
        self._last_loud = 0.0
        self.speaking = False

    @property
    def noise(self) -> float:
        return self._floor.value

    def process(self, pcm: bytes, now: float) -> VadResult:
        level = mean_abs(pcm)
        frame_ms = len(pcm) / BYTES_PER_SECOND * 1000.0
        self._floor.note(level, now)
        onset = False
        ended_at: float | None = None
        if level >= max(self._absolute, self._floor.value * self._ratio):
            self._loud_ms += frame_ms
            self._last_loud = now
            if not self.speaking and self._loud_ms >= self._onset_ms:
                self.speaking = True
                onset = True
        else:
            self._loud_ms = 0.0
            if self.speaking and now - self._last_loud >= self._hangover:
                self.speaking = False
                ended_at = self._last_loud
        return VadResult(speaking=self.speaking, onset=onset, ended_at=ended_at)


class VoiceDetector:
    """Whether a frame of the assistant's audio is voice, not silence or its own noise floor."""

    def __init__(
        self,
        *,
        absolute_threshold: float = OUTPUT_THRESHOLD,
        noise_ratio: float = NOISE_RATIO,
        max_floor: float = OUTPUT_MAX_FLOOR,
    ) -> None:
        self._absolute = absolute_threshold
        self._ratio = noise_ratio
        self._max_floor = max_floor
        self._floor = NoiseFloor()

    def loud(self, pcm: bytes, now: float) -> bool:
        level = mean_abs(pcm)
        self._floor.note(level, now)
        floor = min(self._floor.value, self._max_floor)
        return level >= max(self._absolute, floor * self._ratio)
