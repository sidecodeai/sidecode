"""The gate on a spoken yes, built only from what the gateway itself observes.

``confirm_send`` is the model's tool for sending a drafted message after the person has said
yes. The model is told to wait for a clear yes, but the gateway cannot hear what was said, so it
requires the shape of an answer, in this order, for the draft in question:

1. the model's result for the draft has been handed back (``draft_ready``);
2. at least ``READBACK_SECONDS`` of the assistant's voice then arrives (``voice_out``), summed from
   the draft being handed over: the read-back;
3. the assistant has been silent for at least 0.5 s after the read-back started (the call counts
   it as audible until then), and the person's voice then begins (``person_onset``), seen by
   the gateway's own detector on the microphone;
4. an input transcript arrives after that (``person_transcript``).

The sum counts voice, not words. It cannot tell a read-back from two short remarks that add up to
as much, and a refusal leaves it as it is, so a short read-back that was refused and is then read
again arms it. Voice from before the draft existed (the delegation's acknowledgement) is not in it,
and neither is the silence the service streams between replies.

The person talking while the assistant is speaking, or inside the half second after it, is an
interjection when the model stops for them: the call confirms it as it confirms an interruption
(``barge_in.py``), and only then tells the gate. That spoils the draft for voice confirmation: from
then on ``refusal`` answers ``read_back_interrupted`` whatever the assistant says next, until a new
draft is handed over (``draft_ready``), which starts a fresh sum. The model is told to prepare the
message again and read the new draft back in full; the panel's Send needs no gate and stays open to
the developer. Speech the model talks through (its own voice coming back through a loudspeaker,
say), or that begins after it had already stopped, is neither an interjection nor an answer. The
call holds a confirmation that comes while the model has yet to show which it was until it has,
so the gate is asked only once an interjection over the yes would have spoiled it. A late fragment
of the original request's transcript, and noise heard after the read-back, do not answer it. The
gate stays soft: it cannot tell a yes from a cough.
"""

from __future__ import annotations

#: How much of the assistant's voice makes a read-back: more than an acknowledgement or a remark,
#: less than the shortest message worth reading back.
READBACK_SECONDS = 2.0
#: Voice is summed as fractions of a second; this much short of ``READBACK_SECONDS`` still makes it.
_TOLERANCE = 1e-6


class ConfirmGate:
    def __init__(self) -> None:
        self._draft_id: str | None = None
        self._voiced = 0.0
        self._reading = False
        self._onset = False
        self._answered = False
        self._interrupted = False

    @property
    def voiced_seconds(self) -> float:
        """The voice summed for the draft in question so far."""
        return self._voiced

    def draft_ready(self, draft_id: str) -> None:
        """The model has been handed the draft; what happens from here is the read-back."""
        self._draft_id = draft_id
        self._voiced = 0.0
        self._reading = False
        self._onset = False
        self._answered = False
        self._interrupted = False

    def voice_out(self, seconds: float) -> None:
        """``seconds`` of the assistant's voice arrived; enough of it is the read-back.

        It is summed from the draft being handed over, and not at all once the person cut in.
        """
        if self._draft_id is None or self._answered or self._interrupted:
            return
        self._voiced += seconds
        if self._voiced >= READBACK_SECONDS - _TOLERANCE:
            self._reading = True

    def person_onset(self, *, interrupting: bool) -> None:
        """The person began to speak: after the assistant, or over it in an interruption the model
        confirmed by stopping for them (``interrupting``), which spoils the draft."""
        if self._draft_id is None:
            return
        if interrupting:
            self._interrupted = True
            self._voiced = 0.0
            self._reading = False
            self._onset = False
            self._answered = False
        elif self._reading:
            self._onset = True

    def person_transcript(self) -> None:
        """Words from the person were transcribed. They answer the read-back only after an onset."""
        if self._onset:
            self._answered = True

    def forget(self) -> None:
        """The draft was sent or cancelled: nothing is confirmed any more."""
        self._draft_id = None
        self._voiced = 0.0
        self._reading = False
        self._onset = False
        self._answered = False
        self._interrupted = False

    def refusal(self, draft_id: str) -> str | None:
        """Why a spoken confirmation of this draft is refused, or ``None`` when it may go."""
        if draft_id != self._draft_id:
            return "not_read_back"
        if self._interrupted:
            return "read_back_interrupted"
        if not self._reading:
            return "not_read_back"
        if not self._answered:
            return "not_confirmed"
        return None
