"""What a call used, as OpenAI reports it, ready for one log line per call.

The Live API reports its own audio only as cumulative voice seconds (``session.usage.updated`` and
``session.closed``: each value replaces the last, none is added to another). The delegated backend
reports ordinary Responses usage in the nested ``response.completed`` events, which are counted
once per response id. Numbers only; nothing said or transcribed is ever kept here.
"""

from __future__ import annotations

from typing import Any

#: The Responses events that close a response and carry its ``usage``.
FINAL_RESPONSE_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
MAX_TRACKED_RESPONSES = 512


def is_final_response(inner: dict[str, Any]) -> bool:
    """Whether a nested event closes a response. Its ``type`` is the service's: not always text."""
    kind = inner.get("type")
    return isinstance(kind, str) and kind in FINAL_RESPONSE_EVENTS


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class UsageMeter:
    def __init__(self) -> None:
        #: The latest cumulative voice duration OpenAI reported, and whether ``session.closed``
        #: confirmed it as final.
        self.voice_seconds: float | None = None
        self.final = False
        self.backend_responses = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.reasoning_tokens = 0
        self._seen: set[str] = set()

    def note_voice(self, usage: Any, *, final: bool = False) -> None:
        seconds = usage.get("seconds") if isinstance(usage, dict) else None
        if isinstance(seconds, int | float) and not isinstance(seconds, bool) and seconds >= 0:
            self.voice_seconds = float(seconds)
            self.final = self.final or final

    def note_response(self, inner: dict[str, Any]) -> None:
        """Count one finished backend response, once, from its nested Responses event."""
        if not is_final_response(inner):
            return
        response = inner.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
            return
        identifier = response.get("id")
        if isinstance(identifier, str):
            if identifier in self._seen or len(self._seen) >= MAX_TRACKED_RESPONSES:
                return
            self._seen.add(identifier)
        usage = response["usage"]
        self.backend_responses += 1
        self.input_tokens += _count(usage.get("input_tokens"))
        self.output_tokens += _count(usage.get("output_tokens"))
        self.cached_tokens += _count(_detail(usage, "input_tokens_details", "cached_tokens"))
        self.reasoning_tokens += _count(_detail(usage, "output_tokens_details", "reasoning_tokens"))

    def record(self) -> dict[str, Any]:
        """The numbers for the call's log line."""
        return {
            "voice_seconds": self.voice_seconds,
            "usage_final": self.final,
            "backend_responses": self.backend_responses,
            "backend_input_tokens": self.input_tokens,
            "backend_output_tokens": self.output_tokens,
            "backend_cached_tokens": self.cached_tokens,
            "backend_reasoning_tokens": self.reasoning_tokens,
        }


def _detail(usage: dict[str, Any], group: str, key: str) -> Any:
    details = usage.get(group)
    return details.get(key) if isinstance(details, dict) else None
