"""Drive one Antigravity session in an `agy acp` of the device's own (A64).

One process per session the device runs, started in the session's directory. A new session is
`session/new`; one the device runs no process for opens with `session/resume` or `session/load`
(`opening`), and in every new process the session's own model, thinking level and mode are set
again, since `agy` starts each conversation on the account's defaults. A message is a prompt; a
message sent during a turn steers it, unless a card of the turn is open, when it waits in Up next;
Stop is `session/cancel`. The turn itself is `turnflow`'s, and what `agy` says is `stream`'s.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...attachments import drop
from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from ...models import UNSET, Command, SpeedSetting
from ...sessions.limits import LimitStop
from . import commands as commandlist
from . import errors, exemption, limit, opening, options, publish, settings
from .acp import AgyConnection, AgyError, AgyExited
from .dialogs import Dialogs
from .opening import Opening
from .prompt import build
from .replay import Ledger, Record, Replay
from .stream import Stream
from .translate import Translator
from .turnflow import Hooks, TurnFlow
from .turns import Prompt, Turn

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry
    from .service import AntigravityService

log = logger("rc_client.antigravity")

# How long a turn `agy` began by itself lasts after `agy` last reported the context.
WAKE_QUIET = 3.0
DRAIN_TIMEOUT = 15.0
CLOSE_DRAIN_TIMEOUT = 5.0
# How long a steer waits to hear whether `agy` took it over the turn; it takes one at once.
STEER_WAIT = 5.0
LOAD_TIMEOUT = 120.0

TurnEndCallback = Callable[[], Awaitable[None]]
SessionIdCallback = Callable[[str], Awaitable[None]]


class AntigravityRunner:
    agent = "antigravity"

    def __init__(
        self,
        entry: SessionEntry,
        *,
        binary: str,
        opening: Opening,
        service: AntigravityService,
        on_turn_end: TurnEndCallback | None = None,
        on_session_id: SessionIdCallback | None = None,
    ) -> None:
        self.entry = entry
        self.channel = entry.channel
        self._binary = binary
        self._opening = opening
        self._service = service
        self._on_turn_end = on_turn_end
        self._on_session_id = on_session_id
        session = entry.session
        self._settings = settings.SessionSettings(
            self.channel, settings.Wanted(session.model, session.effort, session.permission_mode)
        )
        self._agy: AgyConnection | None = None
        self._session_id = opening.session_id
        self._commands: list[Command] = []
        self._closed = False
        # Whether the conversation has taken a prompt: until then `agy` keeps nothing of it.
        self._prompted = opening.kind != "new"
        self._translator = Translator()
        self._dialogs = Dialogs(
            self.channel,
            self._respond,
            self._respond_error,
            approves=self._approves_itself,
            session_id=lambda: self._session_id,
        )
        self._record = Record(self.channel.registry, lambda: self._session_id)
        self._turns = TurnFlow(
            self.channel,
            translator=self._translator,
            dialogs=self._dialogs,
            record=self._record,
            usage=dict(session.usage or {}),
            hooks=Hooks(self._limit, self._turn_over, lambda: WAKE_QUIET),
        )
        self._stream = Stream(
            self.channel,
            turns=self._turns,
            translator=self._translator,
            record=self._record,
            on_commands=self._advertised,
            on_options=self._options_changed,
        )

    # ------------------------------------------------------------ the hub's view

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def busy(self) -> bool:
        return self._turns.busy

    @property
    def private_process(self) -> bool:
        """Whether the device may stop this process once nobody has used it for a while (A58).

        Not while `agy` still runs a command in the background or keeps a scheduled wake, and not
        before the conversation took its first prompt: until then a new process could not open it.
        """
        return self._prompted and not self._translator.lingering()

    def _approves_itself(self) -> bool:
        """Bypass permissions (A67), unless an `agy` the device did not start may have the
        conversation open (4.4): then the person is asked, whether or not the device has been able
        to give the conversation up yet."""
        held = self._service.holding.open_elsewhere(self.entry.session.session_id)
        return self._settings.bypassing and not held

    @property
    def supports_steer(self) -> bool:
        return True

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._closed = False
        agy = AgyConnection(
            self._binary,
            cwd=self.channel.session.cwd,
            env=sanitized_child_env(),
            on_notification=self._on_notification,
            on_request=self._on_request,
            on_exit=self._on_exit,
        )
        try:
            await agy.start()
            opening.refuse_old(agy.agent_version)
            started = options.running(await self._open(agy))
            await self._service.options_seen(started)
            await self._settings.start(agy, self._require_id(), started)
        except AgyError as exc:
            await agy.close()
            raise errors.refusal(exc.rpc_code, exc.message) from exc
        except OSError as exc:
            await agy.close()
            raise RcError(
                "agent_unavailable", f"could not start agy: {exc.strerror or exc}"
            ) from exc
        except BaseException:
            await agy.close()
            raise
        self._agy = agy
        await self._settings.publish()

    async def _open(self, agy: AgyConnection) -> dict[str, Any]:
        """Open the session as its plan says, and say what `agy` answered."""
        params: dict[str, Any] = {"cwd": self.channel.session.cwd, "mcpServers": []}
        plan = self._opening
        if plan.kind == "new":
            result = await agy.request("session/new", params)
            session_id = str(result.get("sessionId") or "")
            if not session_id:
                raise RcError("agent_unavailable", "agy did not name the new conversation")
            self._session_id = session_id
            self._record.ledger = Ledger()
            if self._on_session_id is not None:
                await self._on_session_id(session_id)
            exemption.mark(self.channel.registry, session_id)
            return result
        session_id = self._require_id()
        self._record.ledger = Ledger.load(self.channel.registry, session_id)
        params["sessionId"] = session_id
        if plan.kind == "resume":
            try:
                return await agy.request("session/resume", params)
            except AgyError:
                if plan.listed is None:
                    raise
                log.info("agy could not resume a conversation it lists; loading it instead")
        return await self._load(agy, params)

    async def _load(self, agy: AgyConnection, params: dict[str, Any]) -> dict[str, Any]:
        """`session/load`: the replay comes first, and only what was not published yet goes out."""
        walker = self._stream.replay = Replay(self._record.ledger)
        try:
            result = await agy.request("session/load", params, timeout=LOAD_TIMEOUT)
            for row in walker.finish():
                await publish.row(self.channel, row)
        finally:
            self._stream.replay = None
        listed = self._opening.listed
        ledger = self._record.ledger
        if listed is not None:
            ledger.wrote = max(ledger.wrote or 0, listed.updated_ms)
        self._record.save()
        return result

    async def _ensure_open(self) -> None:
        """A process that went away is started again, opening the conversation as any other open."""
        agy = self._agy
        if agy is not None and agy.alive:
            return
        if self._closed or self._session_id is None:
            raise RcError("agent_unavailable", errors.NOT_RUNNING)
        self._opening = await opening.plan(self._service, self.entry, self._binary)
        await self.start()

    async def close(self) -> None:
        """Let go of the process: `agy` saves the conversation as its input closes."""
        self._closed = True
        self._turns.cancel_quiet()
        await self._turns.cancel_prompts()
        await self._dialogs.let_go()
        agy, self._agy = self._agy, None
        if agy is not None:
            await agy.close()
        self._translator.forget()

    async def shutdown(self) -> None:
        """The person closed the session (A39): the turn stops, `agy` closes the conversation.

        A session continued after this is a hand-off like any other (4.4 rule 3).
        """
        if self._session_id is not None:
            exemption.forget(self.channel.registry, self._session_id)
        if self.busy:
            with contextlib.suppress(TimeoutError, RcError):
                await asyncio.wait_for(self.interrupt(), CLOSE_DRAIN_TIMEOUT)
        agy, session_id = self._agy, self._session_id
        if agy is not None and agy.alive and session_id is not None:
            with contextlib.suppress(RcError):
                await agy.request(
                    "session/close", {"sessionId": session_id}, timeout=CLOSE_DRAIN_TIMEOUT
                )
        await self.close()

    async def _on_exit(self, gone: AgyConnection) -> None:
        """`agy` went away on its own: its turn ends in error, and the next message restarts it."""
        if self._agy is not None and self._agy is not gone:
            # A process the runner had already replaced.
            return
        self._agy = None
        async with self._turns.lock:
            if self._turns.turn is not None:
                await self._turns.end("error", errors.STOPPED)
            self._turns.forget_prompts()
        await self._dialogs.let_go()
        self._translator.forget()

    def _require_id(self) -> str:
        if self._session_id is None:
            raise RcError("agent_unavailable", errors.NOT_RUNNING)
        return self._session_id

    # ----------------------------------------------------------------- sending

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        await self._ensure_open()
        prompt = await self._prompt(text, attachments, block_id, source)
        turns = self._turns
        launched = False
        try:
            async with turns.lock:
                await turns.announce(prompt)
                await turns.begin(source)
                self._launch(prompt)
                launched = True
        except BaseException:
            if not launched:
                await drop(prompt.written)
                await turns.unread()
            raise

    async def steer(self, text: str, block_id: str | None = None) -> bool:
        return await self.steer_with_attachments(text, [], block_id)

    async def steer_with_attachments(
        self, text: str, attachments: list[dict[str, Any]], block_id: str | None = None
    ) -> bool:
        """Another prompt into the running turn; False, so it waits in Up next, while a card is up.

        `agy` answers the earlier prompt as it takes this one over, and that is when the message's
        bubble goes out (A14). One `agy` refuses at once is not steered, and nothing of it reached
        `agy`.
        """
        if self._steerable() is None:
            return False
        prompt = await self._prompt(text, attachments, block_id, "remote", steer=True)
        turns = self._turns
        async with turns.lock:
            turn = self._steerable()
            if turn is None:
                await drop(prompt.written)
                return False
            earlier = turns.owner is not None
            self._launch(prompt)
            if not earlier:
                # A turn `agy` began by itself: no prompt of the device's to answer first.
                turn.wake = False
                turns.cancel_quiet()
                await turns.announce(prompt)
                prompt.settle(True)
        try:
            return await asyncio.wait_for(asyncio.shield(prompt.taken), STEER_WAIT)
        except TimeoutError:
            async with turns.lock:
                if not prompt.announced:
                    await turns.announce(prompt)
            return True

    def _steerable(self) -> Turn | None:
        """The turn a message may steer: one is running, `agy` is there, and no card is up."""
        agy = self._agy
        if agy is None or not agy.alive or self._dialogs.pending:
            return None
        return self._turns.turn

    async def _prompt(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None,
        block_id: str | None,
        source: str,
        *,
        steer: bool = False,
    ) -> Prompt:
        built = await build(self.channel.session.session_id, text, attachments)
        return Prompt(
            text=text,
            block_id=block_id or f"user:{uuid.uuid4()}",
            source=source,
            blocks=built.blocks,
            shown=built.shown,
            written=built.written,
            steer=steer,
        )

    def _launch(self, prompt: Prompt) -> None:
        self._turns.launch(prompt, self._run)
        self._prompted = True

    async def _run(self, prompt: Prompt) -> None:
        agy, session_id = self._agy, self._session_id
        if agy is None or session_id is None:
            return
        try:
            result = await agy.request(
                "session/prompt", {"sessionId": session_id, "prompt": prompt.blocks}, timeout=None
            )
        except AgyExited:
            return
        except RcError as exc:
            await self._turns.failed(prompt, exc)
            return
        await self._turns.answered(prompt, result)

    async def _limit(self, text: str, error: str) -> LimitStop | None:
        """A35: a turn whose words read like `agy`'s quota message asks its usage query."""
        if not limit.reads_like_quota(text, error):
            return None
        label = self._settings.now.model_label()
        return await limit.stop(self._service.usage, self._binary, label)

    async def _turn_over(self) -> None:
        """The presence rules decide first: what a holder takes stays queued, not sent (4.4)."""
        await self._service.turn_ended(self.entry)
        if self._on_turn_end is not None:
            await self._on_turn_end()

    # ------------------------------------------------------------------ stops

    async def interrupt(self) -> bool:
        agy, session_id = self._agy, self._session_id
        turns = self._turns
        if agy is None or not agy.alive or session_id is None or turns.turn is None:
            return False
        turns.interrupting = True
        await self.channel.set_state("running", "interrupting")
        try:
            await agy.notify("session/cancel", {"sessionId": session_id})
        except RcError:
            return False
        await self._dialogs.expire_all()
        owner = turns.owner.task if turns.owner is not None else None
        if owner is None:
            async with turns.lock:
                await turns.end("interrupted")
            return True
        try:
            await asyncio.wait_for(asyncio.shield(owner), DRAIN_TIMEOUT)
        except TimeoutError:
            await self._did_not_settle()
        return True

    async def _did_not_settle(self) -> None:
        """`agy` went on after a cancel: its process is started again, and the turn ends here."""
        log.warning("agy did not settle after a cancel")
        await self.channel.notice("warn", errors.DID_NOT_STOP)
        await self.channel.notice("warn", errors.RECONNECTING)
        await self.close()
        self._opening = Opening("resume", self._session_id)
        try:
            await self.start()
        finally:
            async with self._turns.lock:
                await self._turns.end("interrupted")

    # ---------------------------------------------------------------- settings

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        if speed is not UNSET and speed is not None:
            raise RcError("unsupported", errors.NO_SPEED)
        self._settings.ask(model, permission_mode, effort)
        agy, session_id = self._agy, self._session_id
        if agy is None or not agy.alive or session_id is None:
            return
        await self._settings.change(agy, session_id)
        # The hub publishes what was asked for once this returns; what `agy` runs follows it.
        self._turns.spawn(self._publish_after())

    async def _publish_after(self) -> None:
        await asyncio.sleep(0)
        await self._settings.publish()

    async def _options_changed(self, kind: str, update: dict[str, Any]) -> None:
        if self._agy is None:
            # The session is still starting: what it runs is said once its own settings are set.
            return
        await self._settings.heard(kind, update)

    # ---------------------------------------------------------------- commands

    def _advertised(self, listed: list[Command]) -> None:
        if listed != self._commands:
            self._commands = listed
            commandlist.remember(listed)

    async def commands(self) -> list[Command]:
        return list(self._commands) if self._commands else commandlist.recall()

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        """`agy` runs its own commands: the turn's text is `/name argument` (A27)."""
        if not any(command.name == name for command in await self.commands()):
            raise RcError("not_found", f"/{name} is not a command this session offers")
        await self.send(commandlist.typed(name, argument), block_id=block_id)

    # --------------------------------------------------------------- the cards

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool:
        return await self._dialogs.approve(request_id, option_id, message)

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool:
        return await self._dialogs.answer(request_id, answers)

    async def _on_request(self, agy_id: Any, method: str, params: dict[str, Any]) -> None:
        if self._turns.turn is None and self._stream.replay is None:
            await self._turns.wake_if_idle()
        self._turns.cancel_quiet()
        await self._dialogs.request(agy_id, method, params)

    async def _respond(self, agy_id: Any, result: dict[str, Any]) -> None:
        agy = self._agy
        if agy is not None:
            await agy.respond(agy_id, result)

    async def _respond_error(self, agy_id: Any, code: int, message: str) -> None:
        agy = self._agy
        if agy is not None:
            await agy.respond_error(agy_id, code, message)

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        await self._stream.notification(method, params, self._session_id)
