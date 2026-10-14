"""A pi session somebody started in a terminal, driven through the extension.

This is the `shared` half of A26: the same session runner interface the hub
drives an RPC child with, implemented over the socket instead of over a child's
stdin. Everything an app can do on a Codex thread under the shared daemon works
here — send, steer, stop, model, thinking level, permission mode, images and
approvals — because the extension is inside the pi process and can do all of it.

The bubble for a message an app sends is published when pi's own `input` event
comes back, which is the moment the terminal shows it too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting
from ...sessions.channel import SessionChannel
from . import frames, slash
from .adapter import pi_images, usage_from_stats
from .approvals import PiApprovals
from .link import PiLink
from .stream import PiStream, Steer, bubble_id

log = logger("rc_client.pi.terminal")

# How long an abort may take to settle before the device ends the turn itself.
DRAIN_TIMEOUT = 60.0

TurnEndCallback = Callable[[], Awaitable[None]]


class PiTerminalSession:
    """Drives one attached terminal pi session over its extension's link."""

    agent = "pi"

    def __init__(
        self,
        channel: SessionChannel,
        link: PiLink,
        *,
        permission_mode: str,
        on_turn_end: TurnEndCallback | None = None,
    ) -> None:
        self.channel = channel
        self.link = link
        self._permission_mode = permission_mode
        self._stream = PiStream(channel, usage=self._usage, on_turn_end=on_turn_end)
        self.approvals = PiApprovals(channel, link.answer)
        self._context_window: int | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Nothing to start: the pi process dialled us."""

    async def close(self) -> None:
        await self.approvals.expire()
        self.link.detach()

    async def shutdown(self) -> None:
        """A39: the session is the terminal's, so letting go of it is all there is.

        The person is at the pi TUI that owns this session; the apps offer no
        close on a row a terminal holds, and ending their CLI for them is not
        this device's to do.
        """
        await self.close()

    async def gone(self) -> None:
        """pi exited or reloaded: finish anything that was still streaming."""
        await self.approvals.expire()
        await self._stream.torn_down("the pi session ended in the terminal")

    # ------------------------------------------------------------- the link

    async def frame(self, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if kind == frames.EVENT:
            event = frame.get("event")
            if isinstance(event, dict):
                await self._stream.event(event)
            return
        if kind == frames.INPUT:
            await self._input(frame)
            return
        if kind == frames.ASK:
            await self.approvals.ask(frame)
            await self.channel.set_state("needs_approval")
            return
        if kind == frames.ASK_CLOSED:
            await self.approvals.closed(frame)
            await self._settle()

    async def _input(self, frame: dict[str, Any]) -> None:
        """A prompt pi accepted, whoever typed it.

        `interactive` is the person at the keyboard, so the block is a terminal
        message; `extension` is one this device injected and the block is the
        one the app already drew. A steered message keeps A14's rule: its bubble
        waits until pi takes it off the steering queue.
        """
        text = str(frame.get("text") or "")
        source = str(frame.get("source") or "")
        if source == "rpc":
            return
        block_id = str(frame.get("block_id") or "") or bubble_id()
        remote = source == "extension"
        if frame.get("deliver") == "steer":
            self._stream.remember(Steer(text, block_id))
            return
        await self.channel.emit(
            "user_message",
            block_id=block_id,
            text=text,
            source="remote" if remote else "terminal",
        )
        await self._stream.begin("remote" if remote else "terminal")

    async def _settle(self) -> None:
        if self.approvals.waiting:
            await self.channel.set_state("needs_approval")
        else:
            await self.channel.set_state("running" if self.busy else "idle")

    async def _usage(self) -> dict[str, Any] | None:
        try:
            stats = await self.link.command(frames.STATS)
        except RcError:
            return None
        return usage_from_stats(stats, self._context_window)

    # --------------------------------------------------------------- driving

    @property
    def busy(self) -> bool:
        return self._stream.streaming

    @property
    def supports_steer(self) -> bool:
        return True

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        images = pi_images(attachments or [])
        await self._deliver(text, images, bubble_id(block_id), steer=False)

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        if not self.busy:
            return False
        try:
            await self._deliver(text, [], bubble_id(block_id), steer=True)
        except RcError:
            return False
        return True

    async def _deliver(
        self, text: str, images: list[dict[str, str]], block_id: str, steer: bool
    ) -> None:
        fields: dict[str, Any] = {"text": text, "block_id": block_id}
        if images:
            fields["images"] = [
                {"data": image["data"], "mime_type": image["mimeType"]} for image in images
            ]
        if steer:
            fields["deliver"] = "steer"
        await self.link.command(frames.SEND, **fields)

    async def interrupt(self) -> bool:
        if not self.busy:
            return False
        self._stream.interrupting = True
        await self.channel.set_state("running", "interrupting")
        try:
            await self.link.command(frames.ABORT)
        except RcError:
            log.warning("pi refused the abort; waiting for the turn to settle")
        if not await self._stream.settle(DRAIN_TIMEOUT):
            log.warning("pi did not settle after abort; ending the turn")
            await self.channel.notice("warn", "the agent did not stop in time")
            await self._stream.finish({"stop_reason": "interrupted"})
        return True

    async def commands(self) -> list[Command]:
        """The list the extension reads out of the running pi (A27)."""
        try:
            answered = await self.link.command(frames.COMMANDS)
        except RcError:
            return slash.offline()
        return slash.from_agent(answered.get("commands"))

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        listed = await self.commands()
        if not any(command.name == name for command in listed):
            raise RcError("not_found", f"/{name} is not a command this session offers")
        text = slash.typed(name, argument)
        await self.channel.emit(
            "user_message", block_id=bubble_id(block_id), text=text, source="remote"
        )
        if name == slash.COMPACT:
            extra = {"instructions": argument} if argument else {}
            await self.link.command(frames.COMPACT, timeout=slash.COMPACT_TIMEOUT, **extra)
            return
        # The bubble is drawn above rather than from pi's own `input` event,
        # because an extension command raises no `input` at all, so the
        # extension is told to stay quiet about this one injection.
        await self.link.command(frames.SEND, text=text, expand=True, echo=False)

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        if speed is not UNSET and speed is not None:
            raise RcError("unsupported", "pi has no speed tiers")
        if model is not None:
            await self.link.command(frames.SET_MODEL, model=model)
            await self.channel.set_meta(model=model)
        if effort is not None:
            await self.link.command(frames.SET_THINKING, level=effort)
            await self.channel.set_meta(effort=effort)
        if permission_mode is not None:
            await self.link.command(frames.SET_PERMISSION_MODE, mode=permission_mode)
            self._permission_mode = permission_mode
            await self.channel.set_meta(permission_mode=permission_mode)

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        answered = await self.approvals.approve(request_id, option_id)
        if answered:
            await self._settle()
        return answered

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        """pi asks nothing but tool approvals; there is no question to answer."""
        return False
