"""Drive one pi session over its RPC mode on a private `pi --mode rpc` child.

The child loads the device's own extension, which is what gives the session
approvals and its permission mode (A26): pi itself asks nothing. The extension
reaches the daemon over `pi-extension.sock` like any other, announces itself as
an `rpc` session and is told not to stream — the events already arrive on this
process's stdout — so the only traffic on that link is the questions and the
answers to them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting
from ...sessions.channel import SessionChannel
from . import catalog as catalogue
from . import install, paths, slash
from .approvals import PiApprovals
from .rpc import DRAIN_TIMEOUT, PiProcess
from .stream import PiStream, Steer, bubble_id

log = logger("rc_client.pi")

TurnEndCallback = Callable[[], Awaitable[None]]
SessionIdCallback = Callable[[str], Awaitable[None]]

# pi's `prompt` takes base64 images and nothing else, so any other attachment
# is refused rather than silently dropped.
IMAGE_PREFIX = "image/"


class PiRunner:
    agent = "pi"

    def __init__(
        self,
        channel: SessionChannel,
        *,
        binary: str,
        cwd: str,
        session_id: str,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        on_turn_end: TurnEndCallback | None = None,
        on_session_id: SessionIdCallback | None = None,
    ) -> None:
        self.channel = channel
        self._binary = binary
        self._cwd = cwd
        self._session_id = session_id
        self._model = model
        self._effort = effort
        self._permission_mode = permission_mode
        self._on_session_id = on_session_id
        self._process: PiProcess | None = None
        self._stream = PiStream(channel, usage=self._usage, on_turn_end=on_turn_end)
        self.approvals = PiApprovals(channel, self._answer)
        self._context_window: int | None = None
        # Set by the extension service while an extension is attached to this
        # child; the questions travel on it and nothing else does.
        self.link: Any | None = None

    # ------------------------------------------------------------- lifecycle

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    async def start(self) -> None:
        process = PiProcess(
            self._binary,
            cwd=self._cwd,
            args=self._spawn_args(),
            env=self._child_env(),
            on_event=self._stream.event,
            on_closed=self._on_closed,
        )
        await process.start()
        self._process = process
        await self._adopt_state(await process.command("get_state"))

    def _spawn_args(self) -> list[str]:
        """`--session-id` creates the session when it does not exist yet.

        `--no-approve` keeps the device from trusting a project's own pi
        settings, resources and extensions: a remote session must not start
        running code that happens to be checked into the working directory.
        `-e` is passed only when the installed copy of our own extension is not
        the one in this build; the file refuses to act twice in one process.
        """
        args = ["--session-id", self._session_id, "--no-approve"]
        if self._model:
            args.extend(["--model", self._model])
        if self._effort:
            args.extend(["--thinking", self._effort])
        if not install.ready():
            args.extend(["-e", str(paths.bundled_extension())])
        return args

    def _child_env(self) -> dict[str, str]:
        """Tell the extension where to dial and what it is enforcing."""
        env = sanitized_child_env()
        env[paths.SOCKET_ENV] = str(paths.socket_path())
        env[paths.MODE_ENV] = self._permission_mode or "never"
        return env

    async def _adopt_state(self, state: dict[str, Any]) -> None:
        """Report what pi actually runs with, which A17 says apps show."""
        model = state.get("model") if isinstance(state.get("model"), dict) else None
        self._context_window = catalogue.context_window(model)
        fields: dict[str, Any] = {}
        running = catalogue.model_id(model)
        if running:
            self._model = running
            fields["model"] = running
        level = state.get("thinkingLevel")
        if isinstance(level, str) and level:
            self._effort = level
            fields["effort"] = level
        if fields:
            await self.channel.set_meta(**fields)
        session_id = str(state.get("sessionId") or "")
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            if self._on_session_id:
                await self._on_session_id(session_id)

    async def close(self) -> None:
        process = self._process
        self._process = None
        await self.approvals.expire()
        if process is not None:
            await process.close()

    async def shutdown(self) -> None:
        """A39: the pi process is this device's own, so letting go of it ends it."""
        await self.close()

    async def _on_closed(self) -> None:
        await self.approvals.expire()
        await self._stream.torn_down("the pi process exited before the turn completed")

    # ------------------------------------------------------------- approvals

    async def _answer(self, ask_id: str, option_id: str) -> None:
        """Post an app's decision back to the extension holding the tool call."""
        link = self.link
        if link is not None:
            await link.answer(ask_id, option_id)

    async def _usage(self) -> dict[str, Any] | None:
        """pi counts the whole session itself, cost and context window included."""
        process = self._process
        if process is None:
            return None
        try:
            stats = await process.command("get_session_stats")
        except RcError:
            return None
        return usage_from_stats(stats, self._context_window)

    # --------------------------------------------------------------- driving

    @property
    def busy(self) -> bool:
        return self._stream.streaming

    @property
    def supports_steer(self) -> bool:
        """A prompt sent mid-turn is read at the agent's next step, not queued."""
        return True

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        process = self._require_process()
        images = pi_images(attachments or [])
        await self.channel.emit(
            "user_message", block_id=bubble_id(block_id), text=text, source=source
        )
        extra: dict[str, Any] = {"images": images} if images else {}
        await self._prompt(process, text, **extra)
        await self._stream.begin(source)

    async def _prompt(self, process: PiProcess, text: str, **extra: Any) -> None:
        try:
            await process.command("prompt", message=text, **extra)
        except RcError as exc:
            await self.channel.error(exc.message[:2000])
            raise RcError("agent_unavailable", exc.message) from exc

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        process = self._process
        if process is None or not self._stream.streaming:
            return False
        try:
            await process.command("prompt", message=text, streamingBehavior="steer")
        except RcError:
            return False
        # Amendment A14: the bubble waits for pi to take the message off its
        # steering queue, which is the step that reads it.
        self._stream.remember(Steer(text, bubble_id(block_id)))
        return True

    async def interrupt(self) -> bool:
        process = self._process
        if process is None or not self._stream.streaming:
            return False
        self._stream.interrupting = True
        await self.channel.set_state("running", "interrupting")
        try:
            # Stopping means stopping: `abort` alone resumes with whatever is
            # still queued, so the queue goes first (pi's own documented order).
            await process.command("clear_queue")
            await process.command("abort", timeout=DRAIN_TIMEOUT)
        except RcError:
            log.warning("pi refused the abort; waiting for the turn to settle")
        if not await self._stream.settle(DRAIN_TIMEOUT):
            log.warning("pi did not settle after abort; ending the turn")
            await self.channel.notice("warn", "the agent did not stop in time")
            await self._stream.finish({"stop_reason": "interrupted"})
        return True

    async def commands(self) -> list[Command]:
        """What pi says it offers now, or what its files say when it cannot."""
        process = self._process
        if process is None:
            return slash.offline()
        try:
            answered = await process.command("get_commands")
        except RcError:
            return slash.offline()
        return slash.from_agent(answered.get("commands"))

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        """Run one of them (A27), which for pi is one of exactly two things."""
        process = self._require_process()
        listed = await self.commands()
        if not any(command.name == name for command in listed):
            raise RcError("not_found", f"/{name} is not a command this session offers")
        text = slash.typed(name, argument)
        await self.channel.emit(
            "user_message", block_id=bubble_id(block_id), text=text, source="remote"
        )
        if name == slash.COMPACT:
            await self._compact(process, argument)
            return
        # pi expands a prompt template or a skill command into the turn's own
        # text, and runs an extension command outright without any turn at all,
        # so the turn — if there is one — opens on `agent_start` rather than
        # here. Beginning one now would leave an extension command's session
        # running for ever.
        self._stream.trigger = "remote"
        await self._prompt(process, text)

    async def _compact(self, process: PiProcess, argument: str | None) -> None:
        """pi's own `compact`, whose refusal is the message an app shows.

        The `compaction_end` event carries that same refusal, so the stream is
        told to publish this one silently and let the reply speak instead.
        """
        self._stream.own_compaction = True
        extra = {"customInstructions": argument} if argument else {}
        await process.command("compact", timeout=slash.COMPACT_TIMEOUT, **extra)

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        if speed is not UNSET and speed is not None:
            raise RcError("unsupported", "pi has no speed tiers")
        process = self._require_process()
        if model is not None:
            provider, identifier = catalogue.split_model(model)
            await process.command("set_model", provider=provider, modelId=identifier)
            self._model = model
            await self.channel.set_meta(model=model)
        if effort is not None:
            await process.command("set_thinking_level", level=effort)
            self._effort = effort
            await self.channel.set_meta(effort=effort)
        if permission_mode is not None:
            await self._set_permission_mode(permission_mode)

    async def _set_permission_mode(self, mode: str) -> None:
        """The mode is the extension's to enforce, so it is told first."""
        link = self.link
        if link is not None:
            await link.command("set_permission_mode", mode=mode)
        self._permission_mode = mode
        await self.channel.set_meta(permission_mode=mode)

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        return await self.approvals.approve(request_id, option_id)

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        """pi asks nothing but tool approvals; there is no question to answer."""
        return False

    def _require_process(self) -> PiProcess:
        process = self._process
        if process is None:
            raise RcError("agent_unavailable", "the pi session is not connected")
        return process


def pi_images(attachments: list[dict[str, Any]]) -> list[dict[str, str]]:
    """`prompt.images`, which is the only attachment pi takes (A26).

    The shape is pi's own `ImageContent`: `{type, data, mimeType}`, verified
    against a live session rather than taken from the documentation, which also
    shows an older nested `source` form that pi no longer accepts.
    """
    images: list[dict[str, str]] = []
    for item in attachments:
        mime = str(item.get("mime") or item.get("mime_type") or "")
        data = str(item.get("data") or "")
        if not mime.startswith(IMAGE_PREFIX) or not data:
            raise RcError("unsupported", "pi sessions take images and no other attachment")
        images.append({"type": "image", "data": data, "mimeType": mime})
    return images


def usage_from_stats(stats: dict[str, Any], window: int | None) -> dict[str, Any] | None:
    """A turn's totals, from pi's `get_session_stats` or the extension's `stats`."""
    tokens = stats.get("tokens")
    if not isinstance(tokens, dict):
        return None
    usage: dict[str, Any] = {
        "input_tokens": int(tokens.get("input") or 0),
        "output_tokens": int(tokens.get("output") or 0),
        "total_tokens": int(tokens.get("total") or 0),
    }
    cost = stats.get("cost")
    if isinstance(cost, int | float) and cost > 0:
        usage["cost_usd"] = round(float(cost), 8)
    context = stats.get("contextUsage")
    context = context if isinstance(context, dict) else {}
    used = context.get("tokens")
    if isinstance(used, int):
        usage["context_used"] = used
    reported = context.get("contextWindow")
    reported = reported if isinstance(reported, int) else window
    if reported:
        usage["context_window"] = reported
    return usage
