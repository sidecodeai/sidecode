"""The one message the assistant has prepared and is waiting for a yes on."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from .errors import LiveError

DRAFT_TTL_SECONDS = 180.0
MAX_TEXT_CHARS = 2000
MAX_SENDS_PER_CALL = 5


@dataclass
class Draft:
    draft_id: str
    session_id: str
    session_title: str
    text: str
    created_at: float


class DraftBox:
    """At most one pending draft per call, bounded in time and in number of sends.

    Whether a spoken yes may send it is ``gate.py``'s question; this only keeps the draft.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._draft: Draft | None = None
        self.sent = 0

    def prepare(self, session_id: str, title: str, text: str) -> Draft:
        body = text.strip()
        if not body:
            raise LiveError("empty_message", "there is nothing to send")
        if len(body) > MAX_TEXT_CHARS:
            raise LiveError("message_too_long", "the message is too long to send by voice")
        if self.sent >= MAX_SENDS_PER_CALL:
            raise LiveError("send_limit", "this call has reached its limit of sent messages")
        self._draft = Draft(
            draft_id=uuid.uuid4().hex[:12],
            session_id=session_id,
            session_title=title,
            text=body,
            created_at=self._clock(),
        )
        return self._draft

    def pending(self) -> Draft | None:
        draft = self._draft
        if draft is not None and self._clock() - draft.created_at > DRAFT_TTL_SECONDS:
            self._draft = None
        return self._draft

    def take(self, draft_id: str) -> Draft:
        """Claim the pending draft for sending."""
        draft = self.pending()
        if draft is None or draft.draft_id != draft_id:
            raise LiveError("no_such_draft", "that message is no longer waiting")
        self._draft = None
        return draft

    def restore(self, draft: Draft) -> None:
        """Put back a draft whose send failed, unless a newer one has replaced it."""
        if self._draft is None:
            self._draft = draft

    def cancel(self, draft_id: str) -> bool:
        draft = self.pending()
        if draft is None or draft.draft_id != draft_id:
            return False
        self._draft = None
        return True
