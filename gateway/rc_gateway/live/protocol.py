"""The browser's side of ``WS /ws/live``: audio format, frame bounds, message shapes, close codes.

Binary frames carry raw mono signed 16-bit little-endian PCM at ``SAMPLE_RATE``, the rate the
Live API is started with: microphone audio up in frames of 10 to 100 ms (20 ms advised), assistant
audio down in frames of at most 40 ms. Text frames carry small JSON messages, each with a ``type``.

Browser to gateway: ``hangup``, ``captions {enabled}``, ``draft.send {draft_id}``,
``draft.cancel {draft_id}``, ``focus {device_id, session_id}`` (the chat page on screen, both ids
or both ``null``; a hint for the assistant, never a permission) and ``playback {underruns, gap_ms,
restarts, delay_ms}`` (the player's cumulative counters for the call's usage line, and the delay in
milliseconds, 0 to 2000, that it holds each frame before playing it, which the call adds to the end
of the assistant's voice; sent right after ``ready``, then when something changed, and before
``hangup``). Bad values in a ``playback`` are ignored (the three counters only together, the delay
on its own) and in a ``focus`` mean no focus; neither is an error.

Gateway to browser: ``ready {sample_rate, frame_ms, seconds_left}``, ``state {speaker}``,
``working {on}`` (a backend response is in flight or a tool is being answered; sent only when that
changes, and ``ended`` means it is over), ``caption {speaker, delta}``, ``draft {draft}``,
``draft.result {draft_id, ok, session | error}`` (what a press of Send came to; a spoken yes that
sends the draft sends it too, with ``ok`` true, and a refused or failed spoken send sends nothing;
either way it comes before the ``draft`` update that clears the draft), ``flush``, ``notice
{code}`` (the Live API reported an error; the code only, never its text), ``time {seconds_left}``
and, last, ``ended {reason}`` before the socket closes.
"""

from __future__ import annotations

from typing import Any

SAMPLE_RATE = 24_000
BYTES_PER_SECOND = SAMPLE_RATE * 2

#: What the browser is asked to send, and the bounds the gateway enforces on it.
MIC_FRAME_MS = 20
MIN_MIC_FRAME_MS = 10
MAX_MIC_FRAME_MS = 100
#: What the gateway sends down: assistant audio is cut into frames of this length.
OUTPUT_FRAME_MS = 40
#: A text frame from the browser is a short command; anything larger is not one.
MAX_CONTROL_BYTES = 4096

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL_ERROR = 1008
CLOSE_INTERNAL = 1011
CLOSE_NOT_ALLOWED = 4403
CLOSE_CALL_ACTIVE = 4409
CLOSE_BUSY = 4429
CLOSE_UPSTREAM = 4502

#: Refusals and failures that end a call before or without a normal hang-up, by reason.
CLOSE_CODES = {
    "not_allowed": CLOSE_NOT_ALLOWED,
    "call_active": CLOSE_CALL_ACTIVE,
    "cooldown": CLOSE_BUSY,
    "too_busy": CLOSE_BUSY,
    "daily_limit": CLOSE_BUSY,
    "upstream_unavailable": CLOSE_UPSTREAM,
    "upstream_refused": CLOSE_UPSTREAM,
    "upstream_malformed": CLOSE_UPSTREAM,
    "upstream_lost": CLOSE_UPSTREAM,
    "upstream_stalled": CLOSE_UPSTREAM,
    "upstream_errors": CLOSE_UPSTREAM,
    "protocol_error": CLOSE_PROTOCOL_ERROR,
    "gateway_stopping": CLOSE_GOING_AWAY,
    "internal_error": CLOSE_INTERNAL,
}

Message = dict[str, Any]


def close_code_for(reason: str) -> int:
    return CLOSE_CODES.get(reason, CLOSE_NORMAL)


def ready_message(*, seconds_left: int) -> Message:
    return {
        "type": "ready",
        "sample_rate": SAMPLE_RATE,
        "frame_ms": MIC_FRAME_MS,
        "seconds_left": seconds_left,
    }


def ended_message(reason: str) -> Message:
    return {"type": "ended", "reason": reason}


def state_message(speaker: str) -> Message:
    return {"type": "state", "speaker": speaker}


def working_message(on: bool) -> Message:
    return {"type": "working", "on": on}


def caption_message(speaker: str, delta: str) -> Message:
    return {"type": "caption", "speaker": speaker, "delta": delta}


def draft_message(draft: dict[str, str] | None) -> Message:
    return {"type": "draft", "draft": draft}


def draft_sent_message(draft_id: str, session: str | None) -> Message:
    return {"type": "draft.result", "draft_id": draft_id, "ok": True, "session": session}


def draft_failed_message(draft_id: str, error: str) -> Message:
    return {"type": "draft.result", "draft_id": draft_id, "ok": False, "error": error}


def time_message(seconds_left: int) -> Message:
    return {"type": "time", "seconds_left": seconds_left}


def notice_message(code: str) -> Message:
    return {"type": "notice", "code": code}


FLUSH_MESSAGE: Message = {"type": "flush"}
