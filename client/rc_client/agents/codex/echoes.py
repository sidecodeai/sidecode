"""Recognising Codex's echo of a prompt this device sent.

Codex replays every prompt as a `userMessage` item, twice: once on
`item/started` and again on `item/completed`, with the same item id both times.
Neither carries anything that names its author on the way back — the observed
`clientId` of our own injection is `null` — so the text we sent is the only
correlation key, and the item id is what keeps the second event, a backfill and
a reconnect from re-publishing a message the apps already show.

A steered prompt is echoed when the agent reads it rather than when it was
sent, which is why an echo can also carry the block the `user_message` for it
is still owed (amendment A14).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# How many of our own prompts to keep around so Codex's echo of them can be
# recognised; a turn only ever echoes the message that started it.
PENDING = 8
# Item ids already recognised as ours. Larger than `PENDING` because a backfill
# replays items long after their echo was consumed.
RECOGNISED = 64


@dataclass(slots=True, frozen=True)
class Echo:
    """A prompt we sent, and the block its `user_message` is owed under."""

    text: str
    # `None` once the bubble is already out, which is what an ordinary send
    # does: it publishes before it asks Codex anything, so the echo is news to
    # nobody. A steer leaves the block id here instead and the echo publishes.
    block_id: str | None = None


@dataclass(slots=True)
class EchoLog:
    """The prompts this device sent that Codex has yet to echo back."""

    pending: deque[Echo] = field(default_factory=lambda: deque(maxlen=PENDING))
    items: deque[str] = field(default_factory=lambda: deque(maxlen=RECOGNISED))
    clients: set[str] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.pending)

    def remember(self, text: str, block_id: str | None = None) -> Echo | None:
        """Record a prompt we just sent, so its echo can be recognised.

        Answers with the echo this one pushed out of the window when that echo
        still owes a block, so a message can never be silently lost: the caller
        publishes it instead of waiting for a match that will never come.
        """
        crowded = len(self.pending) == PENDING
        evicted = self.pending[0] if crowded and self.pending[0].block_id else None
        self.pending.append(Echo(text, block_id))
        return evicted

    def owns(self, client_id: str) -> bool:
        """Whether this `clientId` is one Codex has stamped on our own echoes."""
        return bool(client_id) and client_id in self.clients

    def recognised(self, item_id: str) -> bool:
        """Whether this item is an echo we have already matched."""
        return bool(item_id) and item_id in self.items

    def claim(self, item_id: str, text: str, client_id: str) -> Echo | None:
        """Consume one pending echo matching `text`, if there is one.

        Only the first match consumes it, so a terminal user typing the same
        words as the message we just sent still gets their own bubble: the echo
        is spent on ours, and theirs arrives as news.
        """
        match = next((echo for echo in self.pending if echo.text == text), None)
        if match is None:
            return None
        self.pending.remove(match)
        if item_id:
            self.items.append(item_id)
        if client_id:
            self.clients.add(client_id)
        return match

    def unclaimed(self) -> list[Echo]:
        """Take the echoes still owing a block, because nothing will match them."""
        held = [echo for echo in self.pending if echo.block_id]
        for echo in held:
            self.pending.remove(echo)
        return held
