"""Recognising the leader's echo of a prompt this device sent.

Every client of the leader sees every prompt as a `user_message_chunk`, whoever
typed it, and nothing in the row names its author. A session attached to the
leader therefore mirrors user messages so the words typed at the TUI reach the
apps — and would mirror its own prompts too, which the apps already drew when
the device sent them. The text is the only correlation key there is, so the
prompts the device sent are held until their echo comes back and is dropped.

Only the first match is consumed, so a person typing the same words as the
device just sent still gets their own bubble: the echo is spent on ours and
theirs arrives as news.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# How many of our own prompts to hold. Grok queues a prompt sent mid-turn, so a
# handful covers every message that can be waiting for its echo at once.
PENDING = 8


@dataclass(slots=True)
class EchoLog:
    """The prompts this device sent that the leader has yet to echo back."""

    pending: deque[str] = field(default_factory=lambda: deque(maxlen=PENDING))

    def __bool__(self) -> bool:
        return bool(self.pending)

    def remember(self, text: str) -> None:
        self.pending.append(text)

    def claim(self, text: str) -> bool:
        """Consume one held prompt matching `text`; True means it was ours."""
        if text not in self.pending:
            return False
        self.pending.remove(text)
        return True
