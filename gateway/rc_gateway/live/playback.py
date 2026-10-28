"""The assistant's audio on its way to the browser: paced, flushable, and silenced on barge-in.

The model may produce speech faster than it is spoken, or stream audio continuously with silence
between the words; either way the Live API offers no way to take back audio already sent ("a
sideband alone does not control playback", the docs say: playback is the client's). So the gateway
holds the audio and releases it at the speed of speech, keeping the browser's queue only
``LEAD_SECONDS`` deep, and it tells voice from silence frame by frame, so that "the assistant is
speaking" means voice and not merely audio. When the person talks over the assistant the gateway
drops what it holds, tells the browser to drop what it has, and discards what the model still sends
while the person is speaking, so the stale part of a reply is never heard.

The lead is as deep as it is because the browser's flush stops everything it has queued at once, so
a deep queue costs nothing when the person interrupts, and it carries playback across a stall in
the network or in the model while the model is ahead of real time. The pacer also counts how often
that cushion was not enough (``starved``), for the call's usage line.

The browser holds each frame for its playout delay before it plays it, so the voice is heard that
much later than the pacer's playhead says. "The assistant is speaking" counts that delay, with what
the browser last reported (``set_playout_delay``) or ``PLAYOUT_DELAY_SECONDS`` before it has.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .protocol import BYTES_PER_SECOND, OUTPUT_FRAME_MS
from .vad import VoiceDetector

#: How far ahead of what the browser is playing the gateway lets the browser's queue run.
LEAD_SECONDS = 1.5
#: How long the browser is assumed to hold each frame before it plays it, until it reports its own.
PLAYOUT_DELAY_SECONDS = 0.4
#: The most audio held back for pacing. Past this the oldest is dropped: a model that has run this
#: far ahead of its own speech is better heard from its newest words.
MAX_HELD_SECONDS = 30.0
#: The assistant counts as speaking this long after the last voiced frame is heard: a pause
#: between words does not end its turn.
VOICE_HANGOVER_SECONDS = 0.5
#: The assistant's audio stays discarded this long after the last loud frame of the interruption...
SUPPRESS_HOLD_SECONDS = 0.3
#: ...and never longer than this from the interruption's start.
SUPPRESS_CAP_SECONDS = 3.0
#: Audio that arrives after the browser's queue ran dry less than this long ago is the model running
#: late in the middle of a reply; after a longer silence it is a new reply.
STARVATION_WINDOW_SECONDS = 2.0
#: A gap shorter than this is not counted. On a stream that arrives at real time it is timer and
#: network jitter, which the browser's playout delay absorbs, and counting it would bury the stalls.
STARVATION_MIN_SECONDS = 0.05

FRAME_BYTES = BYTES_PER_SECOND * OUTPUT_FRAME_MS // 1000
MIN_WAKE_SECONDS = 0.005

SendFrame = Callable[[bytes], Awaitable[None]]
SendFlush = Callable[[], Awaitable[None]]
#: Whether one frame of audio, arriving at this time, is voice.
Classify = Callable[[bytes, float], bool]


def _everything_is_voice(_frame: bytes, _now: float) -> bool:
    return True


def _milliseconds(size: int) -> int:
    return round(size / BYTES_PER_SECOND * 1000)


class OutputPacer:
    """Holds PCM16 audio as frames and says which are due.

    Frames are released so that the browser's queue stays ``lead`` seconds deep.
    """

    def __init__(
        self,
        *,
        lead: float = LEAD_SECONDS,
        max_held_seconds: float = MAX_HELD_SECONDS,
        hangover: float = VOICE_HANGOVER_SECONDS,
        playout_delay: float = PLAYOUT_DELAY_SECONDS,
        classify: Classify = _everything_is_voice,
    ) -> None:
        self._lead = lead
        self._max_bytes = int(max_held_seconds * BYTES_PER_SECOND)
        self._hangover = hangover
        self._classify = classify
        self._held: deque[tuple[bytes, bool]] = deque()
        self._held_bytes = 0
        self._held_voice = 0
        self._carry = b""
        #: When the browser will have played everything sent so far.
        self._playhead = 0.0
        #: When the voice already sent will have been heard.
        self._voice_end = -math.inf
        #: Whether anything has been sent since the browser's queue was last emptied on purpose.
        self._sent = False
        #: What the browser holds each frame before it plays it: the voice is heard that much later
        #: than the playhead says.
        self.playout_delay = playout_delay
        self.dropped_bytes = 0
        #: Bytes of the audio taken in that were voice and not silence, whatever became of them.
        self.voiced_bytes = 0
        #: Times the model's audio arrived after the browser's queue had run dry mid-reply, the
        #: seconds of silence that cost, and the longest of those silences.
        self.starved = 0
        self.starved_seconds = 0.0
        self.starved_max_seconds = 0.0

    @property
    def held_bytes(self) -> int:
        return self._held_bytes

    def add(self, pcm: bytes, now: float) -> bool:
        """Hold audio. Returns whether any of it is voice.

        A chunk that ends mid-sample keeps its odd byte for the next one.
        """
        data = self._carry + pcm
        self._carry = data[-1:] if len(data) % 2 else b""
        if self._carry:
            data = data[:-1]
        if not data:
            return False
        self._note_starvation(now)
        voiced = False
        for start in range(0, len(data), FRAME_BYTES):
            frame = data[start : start + FRAME_BYTES]
            is_voice = self._classify(frame, now)
            self._held.append((frame, is_voice))
            self._held_voice += is_voice
            self._held_bytes += len(frame)
            if is_voice:
                self.voiced_bytes += len(frame)
            voiced = voiced or is_voice
        self._drop_oldest()
        return voiced

    def _note_starvation(self, now: float) -> None:
        """Count audio that arrives when the browser has played everything it was sent.

        Nothing is held, something was sent since the last interruption, and the browser's queue ran
        dry between ``STARVATION_MIN_SECONDS`` and ``STARVATION_WINDOW_SECONDS`` ago: the model's
        audio was late. An estimate, which takes each frame to play in real time from the moment it
        was sent.
        """
        if self._held or not self._sent:
            return
        silence = now - self._playhead
        if STARVATION_MIN_SECONDS <= silence < STARVATION_WINDOW_SECONDS:
            self.starved += 1
            self.starved_seconds += silence
            self.starved_max_seconds = max(self.starved_max_seconds, silence)

    def _drop_oldest(self) -> None:
        while self._held_bytes > self._max_bytes:
            frame, is_voice = self._held.popleft()
            self._held_bytes -= len(frame)
            self._held_voice -= is_voice
            self.dropped_bytes += len(frame)

    def audible(self, now: float) -> bool:
        """Whether voice is held, or has been sent and is still being heard (or just was)."""
        return self.speaking(now, self._hangover)

    def speaking(self, now: float, grace: float) -> bool:
        """Whether voice is held or still being heard, counting ``grace`` seconds past its end.

        The browser plays what it was sent ``playout_delay`` later than the playhead, so the voice
        is heard until its end plus that.
        """
        return self._held_voice > 0 or now < self._voice_end + self.playout_delay + grace

    def due(self, now: float) -> list[bytes]:
        """The frames to send now: as many as keep the browser at most ``lead`` ahead."""
        if not self._held:
            # Only sending moves the playhead, so the silence since the last frame stays measurable.
            return []
        self._playhead = max(self._playhead, now)
        frames: list[bytes] = []
        while self._held and self._playhead - now < self._lead:
            frame, is_voice = self._held.popleft()
            self._held_bytes -= len(frame)
            self._held_voice -= is_voice
            frames.append(frame)
            self._playhead += len(frame) / BYTES_PER_SECOND
            self._sent = True
            if is_voice:
                self._voice_end = self._playhead
        return frames

    def next_wake(self, now: float) -> float | None:
        """Seconds until more is due, or ``None`` while nothing is held."""
        if not self._held:
            return None
        return max(MIN_WAKE_SECONDS, self._playhead - self._lead - now)

    def clear(self, now: float) -> int:
        """Drop what is held, and stand for a browser that has just emptied its own queue."""
        dropped = self._held_bytes
        self._held.clear()
        self._held_bytes = 0
        self._held_voice = 0
        self._carry = b""
        self._playhead = now
        self._voice_end = -math.inf
        self._sent = False
        return dropped


class Playback:
    """The pacer, its sending task, and the rules for an interruption."""

    def __init__(
        self,
        send_frame: SendFrame,
        send_flush: SendFlush,
        *,
        clock: Callable[[], float] = time.monotonic,
        pacer: OutputPacer | None = None,
    ) -> None:
        self._send_frame = send_frame
        self._send_flush = send_flush
        self._clock = clock
        self.pacer = pacer if pacer is not None else OutputPacer(classify=VoiceDetector().loud)
        self._wake = asyncio.Event()
        self._suppress_until = 0.0
        self._interrupted_at: float | None = None
        #: Counts interruptions, so frames already taken from the pacer are not sent after one.
        self._epoch = 0
        self.interruptions = 0
        self.discarded_bytes = 0

    def record(self) -> dict[str, int | None]:
        """What pacing cost, as numbers for the call's usage line."""
        pacer = self.pacer
        return {
            "discarded_audio_ms": _milliseconds(self.discarded_bytes),
            "dropped_audio_ms": _milliseconds(pacer.dropped_bytes),
            "starved": pacer.starved,
            "starved_ms": round(pacer.starved_seconds * 1000),
            "starved_max_ms": round(pacer.starved_max_seconds * 1000) if pacer.starved else None,
        }

    def set_playout_delay(self, seconds: float) -> None:
        """The delay the browser reported: how long it holds a frame before it plays it."""
        self.pacer.playout_delay = seconds

    @property
    def voiced_seconds(self) -> float:
        """How much of the model's audio taken in so far was voice (what was discarded is not)."""
        return self.pacer.voiced_bytes / BYTES_PER_SECOND

    def audible(self) -> bool:
        """Whether the assistant is speaking: voice is on its way to the browser or being heard."""
        return self.pacer.audible(self._clock())

    def speaking(self, grace: float) -> bool:
        """The same, with only ``grace`` seconds counted past the end of the voice."""
        return self.pacer.speaking(self._clock(), grace)

    def add(self, pcm: bytes) -> bool:
        """Take audio from the model, unless the person is talking over the assistant.

        Returns whether it holds voice, as opposed to silence or nothing.
        """
        now = self._clock()
        if now < self._suppress_until:
            self.discarded_bytes += len(pcm)
            return False
        voiced = self.pacer.add(pcm, now)
        self._wake.set()
        return voiced

    async def interrupt(self) -> int:
        """The person began to speak while the assistant was audible: cut it off. Returns bytes."""
        now = self._clock()
        dropped = self.pacer.clear(now)
        self.discarded_bytes += dropped
        self.interruptions += 1
        self._epoch += 1
        self._interrupted_at = now
        self._suppress_until = now + SUPPRESS_HOLD_SECONDS
        await self._send_flush()
        return dropped

    def person_speaking(self) -> None:
        """Keep the assistant's new audio discarded while an interruption goes on."""
        now = self._clock()
        started = self._interrupted_at
        if started is not None and now < started + SUPPRESS_CAP_SECONDS:
            self._suppress_until = max(self._suppress_until, now + SUPPRESS_HOLD_SECONDS)

    async def run(self) -> None:
        """Send audio as it falls due, until cancelled."""
        while True:
            await self._wake.wait()
            while True:
                epoch = self._epoch
                for frame in self.pacer.due(self._clock()):
                    if epoch != self._epoch:
                        break
                    await self._send_frame(frame)
                delay = self.pacer.next_wake(self._clock())
                if delay is None:
                    break
                await asyncio.sleep(delay)
            self._wake.clear()
