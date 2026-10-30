"""Whether the person's speech, begun while the assistant is audible, interrupted it.

The Live API says nothing when the model hears the person and stops (docs/LIVE_ASSISTANT.md,
"Playback and barge-in"), and on a loudspeaker the assistant's own voice, coming back through the
microphone, can start the detector's speech (``vad.py`` asks more of speech while the assistant is
audible, which keeps most of it out). So a speech start while the assistant is audible is only a
suspicion, and the model shows what it was: a model that stops for the person streams silence, or
nothing at all, and the two count the same (``quiet_bytes``). The suspicion is confirmed when the
model, still talking as the person began, goes quiet within ``CONFIRM_WINDOW_SECONDS`` of the speech
start and stays quiet for ``YIELD_SILENCE_SECONDS``. The call is then told, for the read-back gate
(``interjection``), and the assistant is cut off as before (``Playback.interrupt``). Otherwise it
was a false interruption, and nothing is flushed or discarded: the model talked on through the
window, or it had already stopped and only its last words were still being heard. A speech start
before the last one is decided gives that one up as false and is decided in a window of its own.
Whatever waits for the verdict (``settled``, a spoken yes) waits until then.

What it cannot tell: a stall in the stream as long as the quiet, and the end of a sentence or a
pause within half a second of a speech start that was only echo, both look like the model stopping,
and cut off what the browser still had queued.

Each suspicion leaves one line of numbers once it is decided (a confirmed one once its discard is
over, so that the line says all it threw away), and the call's usage line counts the false ones.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..logging import logger
from .playback import Playback
from .protocol import BYTES_PER_SECOND

log = logger("rc_gateway.live.barge_in")

#: How long after the person's speech start the model has to go quiet for them. A real
#: interruption is heard for at most this, and the quiet that confirms it, beyond what cutting it
#: off at once took; a model that has not gone quiet by then was talking on, and a longer window
#: lets more of the pauses between its sentences be taken for stopping.
CONFIRM_WINDOW_SECONDS = 1.0
#: The quiet in the model's stream (silence, or no audio at all) that says it stopped: the half
#: second after which the assistant no longer counts as speaking (``VOICE_HANGOVER_SECONDS``), so a
#: pause between its words is not taken for it.
YIELD_SILENCE_SECONDS = 0.5
#: Clock readings are sums of fractions of a second: this much past the window is still inside it.
_TOLERANCE = 1e-6

Fields = dict[str, int | bool]


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def _audio_ms(size: int) -> int:
    return round(size / BYTES_PER_SECOND * 1000)


@dataclass(frozen=True)
class _Suspicion:
    began: float
    #: From the first voice of what the assistant is saying (as it reached the gateway) to the
    #: speech start.
    segment_ms: int
    #: The mean absolute amplitude (of 32768) of the loud run that started the person's speech.
    start_level: int
    #: The model's voice taken in before the speech start.
    voiced_bytes: int


@dataclass(frozen=True)
class _Cut:
    """A confirmed interruption whose discard is still running: its line waits for the total."""

    fields: Fields
    discarded_before: int


class BargeIn:
    """The speech starts over the assistant, each held until the model shows what it was."""

    def __init__(
        self,
        playback: Playback,
        *,
        call_id: str,
        flush: bool,
        interjection: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        window: float = CONFIRM_WINDOW_SECONDS,
        silence: float = YIELD_SILENCE_SECONDS,
    ) -> None:
        self._playback = playback
        self._call_id = call_id
        self._flush = flush
        self._interjection = interjection
        self._clock = clock
        self._window = window
        self._silence_bytes = round(silence * BYTES_PER_SECOND)
        self._cut: _Cut | None = None
        #: The speech start waiting to be decided; set and cleared only by ``_hold`` and
        #: ``_release``, so that ``_decided`` is set exactly while there is none.
        self._suspicion: _Suspicion | None = None
        self._decided = asyncio.Event()
        self._decided.set()
        #: Speech starts over the assistant that it did not stop for.
        self.false_interruptions = 0

    def person_began(self, level: float) -> bool:
        """The person's speech began, its first loud run at ``level``.

        Returns whether the assistant was audible, so that the speech may be interrupting it; if
        not, the person is speaking after it. One still undecided is given up as false: the model
        talked on until this speech start, which is decided in a window of its own.
        """
        playback = self._playback
        if not playback.audible():
            return False
        now = self._clock()
        if self._suspicion is not None:
            self._dismiss(self._suspicion, now)
        stream = playback.pacer
        started = stream.segment_started
        suspicion = _Suspicion(
            began=now,
            segment_ms=_ms(now - started) if started is not None else 0,
            start_level=round(level),
            voiced_bytes=stream.voiced_bytes,
        )
        playback.end_interruption()
        if stream.quiet_bytes(now) >= self._silence_bytes:
            # It had already stopped: only its last words are still being heard.
            self._dismiss(suspicion, now)
        else:
            self._hold(suspicion)
        return True

    async def settle(self) -> bool:
        """Decide the suspicion once the model has shown what it was, and cut the assistant off for
        one it stopped for. Returns whether an interruption was confirmed now.

        Quiet that began inside the window counts once it is long enough, however long after the
        window that is; only a model still talking as the window closed was not interrupted.
        """
        self._report_cut()
        suspicion = self._suspicion
        if suspicion is None:
            return False
        now = self._clock()
        quiet = self._playback.pacer.quiet_bytes(now)
        if now - quiet / BYTES_PER_SECOND > suspicion.began + self._window + _TOLERANCE:
            self._dismiss(suspicion, now)
            return False
        if quiet < self._silence_bytes:
            return False
        await self._confirm(suspicion, now)
        return True

    async def settled(self) -> None:
        """Return once no speech over the assistant waits to be decided."""
        while self._suspicion is not None:
            await self._decided.wait()

    def close(self) -> None:
        """The call is ending: every line is written, and what is undecided was not confirmed."""
        cut, self._cut = self._cut, None
        if cut is not None:
            self._write(cut.fields, self._playback.discarded_bytes - cut.discarded_before)
        if self._suspicion is not None:
            self._dismiss(self._suspicion, self._clock())

    def _hold(self, suspicion: _Suspicion) -> None:
        self._suspicion = suspicion
        self._decided.clear()

    def _release(self) -> None:
        self._suspicion = None
        self._decided.set()

    async def _confirm(self, suspicion: _Suspicion, now: float) -> None:
        fields = self._fields(suspicion, now, confirmed=True)
        # The call hears of it before anything waiting for the verdict goes on.
        self._interjection()
        self._release()
        if not self._flush:
            self._write(fields, 0)
            return
        self._cut = _Cut(fields, self._playback.discarded_bytes)
        await self._playback.interrupt()

    def _dismiss(self, suspicion: _Suspicion, now: float) -> None:
        self._release()
        self.false_interruptions += 1
        self._write(self._fields(suspicion, now, confirmed=False), 0)

    def _report_cut(self) -> None:
        """Write a confirmed interruption down once its discard is over."""
        cut = self._cut
        if cut is not None and not self._playback.discarding():
            self._cut = None
            self._write(cut.fields, self._playback.discarded_bytes - cut.discarded_before)

    def _fields(self, suspicion: _Suspicion, now: float, *, confirmed: bool) -> Fields:
        voiced = self._playback.pacer.voiced_bytes - suspicion.voiced_bytes
        return {
            "confirmed": confirmed,
            "segment_ms": suspicion.segment_ms,
            "start_level": suspicion.start_level,
            "wait_ms": _ms(now - suspicion.began),
            "voice_ms": _audio_ms(voiced),
        }

    def _write(self, fields: Fields, discarded: int) -> None:
        log.info(
            "live barge-in", call_id=self._call_id, **fields, discarded_ms=_audio_ms(discarded)
        )
