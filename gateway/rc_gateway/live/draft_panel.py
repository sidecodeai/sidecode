"""The panel's side of the assistant's draft: the card it shows, and what became of a send.

The panel shows the one draft the assistant is waiting for a yes on, and its Send and Cancel buttons
answer here. The card is kept in step with the draft box, except while a send is under way: the
computer has the message until it replies, so what ends the send (a press of Send, or a spoken yes)
says what became of it with ``draft.result`` before the panel learns that the card is gone, and a
send that fails leaves the card where it was.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .browser import BrowserLink
from .drafts import DraftBox
from .errors import LiveError
from .gate import ConfirmGate
from .protocol import draft_failed_message, draft_message, draft_sent_message
from .tools import ToolRunner, sent_draft

#: Quiet context for the voice model, said when a button settled the draft.
TellModel = Callable[[str], Awaitable[None]]


class DraftPanel:
    def __init__(
        self,
        *,
        browser: BrowserLink,
        drafts: DraftBox,
        gate: ConfirmGate,
        tools: ToolRunner,
        tell_model: TellModel,
    ) -> None:
        self._browser = browser
        self._drafts = drafts
        self._gate = gate
        self._tools = tools
        self._tell_model = tell_model
        self._published: str | None = None
        self._delivered: str | None = None
        #: Sends under way. While there is one the card is left as it is.
        self._sends = 0

    async def send(self, draft_id: str) -> None:
        """The panel's Send button."""
        self.begin_send()
        try:
            try:
                result = await self._tools.send_draft(draft_id, spoken=False)
            except LiveError as exc:
                reply = draft_failed_message(draft_id, exc.code)
            else:
                reply = draft_sent_message(draft_id, result.get("session"))
                await self._tell_model("The developer sent the pending draft with the Send button.")
            await self._browser.send_json(reply)
        finally:
            self.end_send()
        await self.publish()

    async def cancel(self, draft_id: str) -> None:
        """The panel's Cancel button."""
        if self._drafts.cancel(draft_id):
            self._gate.forget()
            await self._tell_model(
                "The developer cancelled the pending draft with the Cancel button."
            )
        await self.publish()

    def begin_send(self) -> None:
        """A send is under way: the card stays until what ends it has been said."""
        self._sends += 1

    def end_send(self) -> None:
        self._sends -= 1

    async def announce(self, output: str) -> None:
        """A tool's answer says a spoken yes sent the draft: tell the panel as Send does."""
        sent = sent_draft(output)
        if sent is not None:
            await self._browser.send_json(draft_sent_message(*sent))

    async def publish(self) -> None:
        """Show the card as the draft box holds it now, when that is not what the panel was told."""
        if self._sends:
            return
        draft = self._drafts.pending()
        identifier = draft.draft_id if draft is not None else None
        if identifier == self._published:
            return
        self._published = identifier
        shown = (
            None
            if draft is None
            else {"id": draft.draft_id, "session": draft.session_title, "text": draft.text}
        )
        await self._browser.send_json(draft_message(shown))

    def deliver(self) -> None:
        """A draft the model has just been handed starts the gate on its read-back."""
        draft = self._drafts.pending()
        if draft is not None and draft.draft_id != self._delivered:
            self._delivered = draft.draft_id
            self._gate.draft_ready(draft.draft_id)
