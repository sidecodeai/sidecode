"""One call: the browser's audio to the Live API, replies and tool calls back, every way out.

The call owns both sockets and is the only thing that closes them, in one place (``_finish``), so
no exit path can leave the other side open: when the browser goes, the Live API socket is closed at
once; when the Live API socket drops, the browser is told why and closed; when the gateway stops,
both are. Neither side can hold the other up: the browser's farewell and the Live API's close run
side by side, each bounded. Nothing said or transcribed is stored or logged. A transcript event
only tells the call that someone is speaking, and is passed on to the browser (when it asked for
captions) and dropped. The call also reports its open time to a meter (``meter.py``), which can end
it, and whatever the meter has to pay for is paid when the call ends, however it ends.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from ..logging import logger
from .audio import MicFrameGate
from .browser import BrowserLink
from .config import LiveConfig
from .draft_panel import DraftPanel
from .drafts import DraftBox
from .errors import LiveError, safe_code
from .focus import parse_focus
from .gate import ConfirmGate
from .limits import MAX_COMMANDS_IN_FLIGHT, CommandGate
from .meter import CallMeter, NoMeter
from .playback import Playback
from .protocol import (
    FLUSH_MESSAGE,
    caption_message,
    notice_message,
    ready_message,
    state_message,
    time_message,
    working_message,
)
from .stats import InputMeter, ReplyTimer
from .telemetry import PlaybackReport
from .tools import SEND_TOOL, ToolRunner
from .upstream import UpstreamLink
from .usage import UsageMeter
from .vad import EnergyVad, VadResult
from .work import WorkTracker

log = logger("rc_gateway.live.call")

WATCH_INTERVAL_SECONDS = 1.0
TIME_TICK_SECONDS = 3.0
#: The microphone streams all the time; a browser that sends nothing for this long is not there.
BROWSER_SILENT_SECONDS = 15.0
#: How long the Live API gets to confirm a close, and so to report final usage.
CLOSE_GRACE_SECONDS = 5.0
#: What a step of ending a call may take (the browser's farewell, stopping tasks) before it is
#: given up on, and the room the Live API's close gets beyond its grace for the send itself.
STEP_TIMEOUT_SECONDS = 8.0
UPSTREAM_CLOSE_SLACK_SECONDS = 4.0
CAPTION_MAX_CHARS = 500
MIN_EXPIRY_SECONDS = 5.0
#: The assistant counts as speaking until this long after its last voice has been heard. A person's
#: voice that begins sooner (a sound in a pause between the read-back's sentences, a reply on top
#: of its last words) interrupts the read-back; only after this much silence does it answer it.
GATE_GRACE_SECONDS = 0.5
#: Errors from the Live API in a row, with nothing else heard between, after which the call ends.
MAX_CONSECUTIVE_ERRORS = 3

SPEAKER_USER = "user"
SPEAKER_ASSISTANT = "assistant"
SPEAKER_NONE = "none"

#: ``session.closed`` reasons the call names itself; any other is a lost connection.
UPSTREAM_END_REASONS = {"expired": "expired", "content": "content"}

OnEnd = Callable[["LiveCall"], Awaitable[None]]


class LiveCall:
    def __init__(
        self,
        *,
        username: str,
        browser: BrowserLink,
        upstream: UpstreamLink,
        started: dict[str, Any],
        config: LiveConfig,
        tools: ToolRunner,
        drafts: DraftBox,
        gate: ConfirmGate,
        on_end: OnEnd,
        meter: CallMeter | None = None,
        budget_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        watch_interval: float = WATCH_INTERVAL_SECONDS,
        close_grace: float = CLOSE_GRACE_SECONDS,
        step_timeout: float = STEP_TIMEOUT_SECONDS,
        browser_silence: float = BROWSER_SILENT_SECONDS,
    ) -> None:
        self.call_id = uuid.uuid4().hex
        self.username = username
        self.session_id = _session_id(started)
        self.end_reason: str | None = None
        self.drafts = drafts
        self.usage = UsageMeter()
        self._browser = browser
        self._upstream = upstream
        self._config = config
        self._tools = tools
        self._gate = gate
        self._on_end = on_end
        self._meter = meter if meter is not None else NoMeter()
        self._clock = clock
        self._watch_interval = watch_interval
        self._close_grace = close_grace
        self._step_timeout = step_timeout
        self._browser_silence = browser_silence
        self._started = clock()
        limit = _call_seconds(config, started, wall_clock)
        self._limit_reason = "max_duration"
        if budget_seconds is not None and budget_seconds < limit:
            limit, self._limit_reason = budget_seconds, "daily_limit"
        self._deadline = self._started + limit
        self._last_activity = self._started
        self._mic_gate = MicFrameGate(clock=clock)
        self._command_gate = CommandGate(clock=clock)
        self._vad = EnergyVad()
        self._playback = Playback(self._browser.send_bytes, self._send_flush, clock=clock)
        self._replies = ReplyTimer()
        self._input = InputMeter()
        self._work = WorkTracker()
        self._browser_playback = PlaybackReport()
        self._captions = False
        self._speaker = SPEAKER_NONE
        self._working = False
        self._panel = DraftPanel(
            browser=browser, drafts=drafts, gate=gate, tools=tools, tell_model=self._tell_model
        )
        self._errors = 0
        self._busy: set[asyncio.Task[None]] = set()
        self._commands: set[asyncio.Task[None]] = set()
        self._browser_task: asyncio.Task[None] | None = None
        self._upstream_task: asyncio.Task[None] | None = None
        self._side_tasks: list[asyncio.Task[None]] = []
        self._finish_task: asyncio.Task[None] | None = None
        self._answered: set[str] = set()
        self._upstream_closed = asyncio.Event()
        self._done = asyncio.Event()
        self._meter.start(self.call_id)

    @property
    def active(self) -> bool:
        return self._finish_task is None

    def duration(self) -> float:
        """Seconds since the call began."""
        return self._clock() - self._started

    # ---- lifecycle ----

    async def run(self) -> None:
        """Serve the call until it ends, whichever way it ends."""
        log.info("live call started", account=self.username, call_id=self.call_id)
        try:
            await self._browser.send_json(self._ready())
            self._browser_task = self._start(self._read_browser())
            self._upstream_task = self._start(self._read_upstream())
            self._side_tasks = [self._start(self._playback.run()), self._start(self._watch())]
            await self._done.wait()
        except LiveError as exc:
            self.request_finish(exc.code)
        finally:
            # A cancelled handler still closes both sockets: the closing is a task of its own.
            await self.finish("cancelled")

    def request_finish(self, reason: str) -> None:
        """Begin ending the call, without waiting for it. The first reason given is kept."""
        if self._finish_task is None:
            self.end_reason = reason
            self._finish_task = asyncio.create_task(self._finish())

    async def finish(self, reason: str) -> None:
        """End the call, and return once both sockets are closed (or given up on)."""
        self.request_finish(reason)
        assert self._finish_task is not None
        await asyncio.shield(self._finish_task)

    async def _finish(self) -> None:
        """Every step runs whatever the one before it did: no failure may leave a socket open.

        The browser's farewell and the Live API's close run side by side: a browser that has
        stopped reading cannot keep the session open (and billing), or the call slot held.
        """
        try:
            await self._attempt(
                self._stop_tasks([self._browser_task, *self._side_tasks, *self._busy]),
                self._step_timeout,
            )
            await self._attempt(self._on_end(self), self._step_timeout)
            await asyncio.gather(
                self._attempt(self._browser.end(self.end_reason or "closed"), self._step_timeout),
                self._attempt(
                    self._close_upstream(), self._close_grace + UPSTREAM_CLOSE_SLACK_SECONDS
                ),
            )
        finally:
            await self._close_books()

    async def _close_books(self) -> None:
        """The last step, whatever went before it: pay for the open seconds, then say what it cost.

        The meter is stopped first and without waiting, so even a call whose ending was cut short
        pays for the time it was open. Its answer is waited for, bounded, only so that the usage
        line can say how the last payment came out.
        """
        try:
            self._stop_meter()
            await self._attempt(self._meter.settled(), self._step_timeout)
        finally:
            self._log_usage()
            self._done.set()

    def _stop_meter(self) -> None:
        try:
            self._meter.stop(self.duration())
        except Exception as exc:
            log.warning("live meter failed to stop", call_id=self.call_id, error=type(exc).__name__)

    async def _attempt(self, step: Awaitable[None], timeout: float) -> None:
        try:
            await asyncio.wait_for(step, timeout)
        except TimeoutError:
            log.warning("live call step timed out", call_id=self.call_id)
        except Exception as exc:
            log.warning("live call step failed", call_id=self.call_id, error=type(exc).__name__)

    async def _stop_tasks(self, tasks: list[asyncio.Task[None] | None]) -> None:
        live = [task for task in tasks if task is not None and not task.done()]
        for task in live:
            task.cancel()
        if live:
            await asyncio.wait(live, timeout=self._step_timeout)

    async def _close_upstream(self) -> None:
        """Ask for a graceful close, wait briefly for the final usage, then close the socket.

        The final usage is read by the upstream reader; a call that ended before that reader
        started has nobody to hear it, so it does not wait.
        """
        try:
            if not self._upstream_closed.is_set():
                with contextlib.suppress(LiveError):
                    await self._upstream.send({"type": "session.close", "event_id": "close"})
                    if self._upstream_task is not None and not self._upstream_task.done():
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._upstream_closed.wait(), self._close_grace)
        finally:
            await self._attempt(self._stop_tasks([self._upstream_task]), self._step_timeout)
            await self._attempt(self._upstream.close(), self._step_timeout)

    def _log_usage(self) -> None:
        log.info(
            "live call usage",
            account=self.username,
            call_id=self.call_id,
            session_id=self.session_id,
            reason=self.end_reason,
            seconds=int(self.duration()),
            interruptions=self._playback.interruptions,
            messages_sent=self.drafts.sent,
            **self.usage.record(),
            **self._playback.record(),
            **self._replies.record(),
            **self._input.record(),
            **self._work.record(),
            **self._browser_playback.record(),
            **self._meter.record(),
        )

    def _ready(self) -> dict[str, Any]:
        return ready_message(seconds_left=self._seconds_left())

    def _seconds_left(self) -> int:
        return max(0, int(self._deadline - self._clock()))

    def _start(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run a task of the call. Whatever fails in it ends the call, with the reason it gives."""
        task = asyncio.create_task(work)
        task.add_done_callback(self._supervise)
        return task

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run short work (a tool call, a button) that holds off the idle hang-up while it runs."""
        task = self._start(work)
        self._busy.add(task)
        task.add_done_callback(self._busy.discard)
        return task

    def _supervise(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if isinstance(error, LiveError):
            self.request_finish(error.code)
        elif error is not None:
            log.warning("live call task failed", call_id=self.call_id, error=type(error).__name__)
            self.request_finish("internal_error")

    # ---- the browser ----

    async def _read_browser(self) -> None:
        while True:
            try:
                message = await asyncio.wait_for(self._browser.receive(), self._browser_silence)
            except TimeoutError:
                self.request_finish("browser_silent")
                return
            if message is None:
                self.request_finish("browser_closed")
                return
            if isinstance(message, bytes):
                await self._on_mic(message)
            else:
                self._command_gate.check()
                await self._on_command(message)

    async def _on_mic(self, frame: bytes) -> None:
        self._mic_gate.check(frame)
        now = self._clock()
        heard = self._vad.process(frame, now)
        self._time_replies(heard)
        if heard.speaking:
            self._input.speech(len(frame))
            self._last_activity = now
            if heard.onset:
                # Asked before any interruption clears what is playing.
                self._gate.person_onset(
                    assistant_speaking=self._playback.speaking(GATE_GRACE_SECONDS)
                )
            if self._config.barge_in:
                if heard.onset and self._playback.audible():
                    await self._playback.interrupt()
                self._playback.person_speaking()
        await self._refresh_state()
        audio = base64.b64encode(frame).decode("ascii")
        await self._upstream.send({"type": "session.input_audio.append", "audio": audio})

    def _on_playback(self, command: dict[str, Any]) -> None:
        """Keep the player's report, and take its playout delay into the assistant's voice."""
        self._browser_playback.note(command)
        delay = self._browser_playback.delay_seconds
        if delay is not None:
            self._playback.set_playout_delay(delay)

    def _time_replies(self, heard: VadResult) -> None:
        """Run the clock on the assistant's answer from the end of the person's speech."""
        if heard.onset:
            self._replies.person_began()
        if heard.ended_at is not None:
            self._replies.person_stopped(heard.ended_at, assistant_audible=self._playback.audible())

    async def _on_command(self, command: dict[str, Any]) -> None:
        kind = command.get("type")
        draft_id = command.get("draft_id")
        if kind == "hangup":
            self.request_finish("hangup")
        elif kind == "captions":
            self._captions = command.get("enabled") is True
        elif kind == "focus":
            self._tools.focus = parse_focus(command)
        elif kind == "playback":
            self._on_playback(command)
        elif kind == "draft.send" and isinstance(draft_id, str):
            self._spawn_command(self._panel.send(draft_id))
        elif kind == "draft.cancel" and isinstance(draft_id, str):
            self._spawn_command(self._panel.cancel(draft_id))

    def _spawn_command(self, work: Coroutine[Any, Any, None]) -> None:
        """A button's work. A browser that leaves several unfinished is not pressing buttons."""
        if len(self._commands) >= MAX_COMMANDS_IN_FLIGHT:
            work.close()
            raise LiveError("protocol_error", "too many commands at once")
        task = self._spawn(work)
        self._commands.add(task)
        task.add_done_callback(self._commands.discard)

    async def _refresh_state(self) -> None:
        if self._vad.speaking:
            speaker = SPEAKER_USER
        else:
            speaker = SPEAKER_ASSISTANT if self._playback.audible() else SPEAKER_NONE
        if speaker != self._speaker:
            self._speaker = speaker
            await self._browser.send_json(state_message(speaker))

    async def _send_flush(self) -> None:
        await self._browser.send_json(FLUSH_MESSAGE)

    # ---- quiet context for the voice model ----

    async def _tell_model(self, text: str) -> None:
        """Quiet, application-written context for the voice model (never the person's words)."""
        event = {
            "type": "session.thinking.append",
            "event_id": uuid.uuid4().hex,
            "delegation_id": None,
            "content": text,
        }
        with contextlib.suppress(LiveError):
            await self._upstream.send(event)

    # ---- the Live API ----

    async def _read_upstream(self) -> None:
        while True:
            try:
                event = await self._upstream.receive()
            except LiveError as exc:
                self._upstream_closed.set()
                self.request_finish(exc.code)
                return
            try:
                await self._on_upstream(event)
            except LiveError as exc:
                self.request_finish(exc.code)

    async def _on_upstream(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind != "error":
            self._errors = 0
        if kind == "session.usage.updated":
            self.usage.note_voice(event.get("usage"))
        elif kind == "session.closed":
            self._on_session_closed(event)
        elif kind == "response.event":
            self._on_backend_event(event.get("event"))
            await self._publish_working()
        elif not self.active:
            return
        elif kind == "session.output_audio.delta":
            self._on_audio(event.get("delta"))
        elif kind == "session.output_transcript.delta":
            await self._on_transcript(SPEAKER_ASSISTANT, event.get("delta"))
        elif kind == "session.input_transcript.delta":
            await self._on_transcript(SPEAKER_USER, event.get("delta"))
        elif kind == "error":
            await self._on_service_error(event)

    async def _on_service_error(self, event: dict[str, Any]) -> None:
        """Tell the browser something went wrong (the code, never the text) and count it."""
        error = event.get("error")
        detail = error if isinstance(error, dict) else {}
        code = safe_code(detail.get("code"))
        log.warning(
            "live service error",
            call_id=self.call_id,
            error_code=code,
            error_type=safe_code(detail.get("type")),
            param=safe_code(detail.get("param")),
        )
        self._errors += 1
        await self._browser.send_json(notice_message(code))
        if self._errors >= MAX_CONSECUTIVE_ERRORS:
            self.request_finish("upstream_errors")

    def _on_session_closed(self, event: dict[str, Any]) -> None:
        self.usage.note_voice(event.get("usage"), final=True)
        self._upstream_closed.set()
        reason = str(event.get("reason") or "")
        self.request_finish(UPSTREAM_END_REASONS.get(reason, "upstream_lost"))

    def _on_audio(self, delta: Any) -> None:
        if not isinstance(delta, str):
            return
        try:
            pcm = base64.b64decode(delta, validate=True)
        except (binascii.Error, ValueError):
            return
        # Silence the model streams is not activity; only voice keeps the call from going idle.
        voiced_before = self._playback.voiced_seconds
        if self._playback.add(pcm):
            now = self._clock()
            self._last_activity = now
            self._replies.voice_arrived(now)
            self._gate.voice_out(self._playback.voiced_seconds - voiced_before)

    async def _on_transcript(self, speaker: str, delta: Any) -> None:
        self._last_activity = self._clock()
        if speaker == SPEAKER_USER:
            self._input.transcript()
            self._gate.person_transcript()
        if self._captions and isinstance(delta, str) and delta:
            await self._browser.send_json(caption_message(speaker, delta[:CAPTION_MAX_CHARS]))

    def _on_backend_event(self, inner: Any) -> None:
        if not isinstance(inner, dict):
            return
        self.usage.note_response(inner)
        self._work.note_event(inner, self._clock())
        item = inner.get("item")
        if (
            self.active
            and inner.get("type") == "response.output_item.done"
            and isinstance(item, dict)
            and item.get("type") == "function_call"
            and self._first_time(item.get("call_id"))
        ):
            self._work.tool_started()
            self._spawn(self._answer_tool(item))

    def _first_time(self, call_id: Any) -> bool:
        """False for a function call this call has already taken up."""
        if not isinstance(call_id, str):
            return True
        if call_id in self._answered:
            return False
        self._answered.add(call_id)
        return True

    async def _answer_tool(self, item: dict[str, Any]) -> None:
        started = self._clock()
        self._last_activity = started
        name = str(item.get("name"))
        sends = name == SEND_TOOL
        if sends:
            self._panel.begin_send()
        try:
            output = await self._tools.run(name, str(item.get("arguments") or ""))
            self._work.tool_ran(self._clock() - started)
            if not self.active:
                return
            await self._panel.announce(output)
            await self._hand_back(item, output)
        finally:
            self._work.tool_ended()
            if sends:
                self._panel.end_send()
        await self._panel.publish()
        await self._publish_working()

    async def _hand_back(self, item: dict[str, Any], output: str) -> None:
        """Give the backend a tool's answer and ask it to go on."""
        self._last_activity = self._clock()
        result = {"type": "function_call_output", "call_id": item.get("call_id"), "output": output}
        await self._upstream.send({"type": "response.item.create", "item": result})
        await self._upstream.send({"type": "response.create"})
        self._work.continuation_asked(self._clock())
        self._panel.deliver()

    async def _publish_working(self) -> None:
        """Tell the browser when the backend starts or stops working, and only then."""
        working = self._work.working(self._clock())
        if working != self._working:
            self._working = working
            await self._browser.send_json(working_message(working))

    # ---- limits ----

    async def _watch(self) -> None:
        last_tick = self._started
        while True:
            await asyncio.sleep(self._watch_interval)
            now = self._clock()
            self._meter.tick(now - self._started)
            reason = self._meter.verdict()
            if reason is not None:
                self.request_finish(reason)
                return
            if now >= self._deadline:
                self.request_finish(self._limit_reason)
                return
            if self._idle(now):
                self.request_finish("idle")
                return
            await self._panel.publish()
            await self._publish_working()
            if now - last_tick >= TIME_TICK_SECONDS:
                last_tick = now
                await self._browser.send_json(time_message(self._seconds_left()))

    def _idle(self, now: float) -> bool:
        """Nobody has spoken, nothing is being said or run, for the idle limit."""
        quiet = not self._busy and not self._playback.audible() and not self._vad.speaking
        return quiet and now - self._last_activity >= self._config.idle_seconds


def _session_id(started: dict[str, Any]) -> str:
    session = started.get("session")
    identifier = session.get("id") if isinstance(session, dict) else None
    return identifier if isinstance(identifier, str) else ""


def _call_seconds(config: LiveConfig, started: dict[str, Any], wall: Callable[[], float]) -> float:
    """The configured cap, or less when the service says the session expires sooner."""
    session = started.get("session")
    expires = session.get("expires_at") if isinstance(session, dict) else None
    if isinstance(expires, int | float) and not isinstance(expires, bool):
        left = expires - wall()
        # A moment that is already past, or about to be, is not a limit to plan a call around: the
        # service ends the session itself when it expires.
        if left >= MIN_EXPIRY_SECONDS:
            return min(config.max_call_seconds, left)
    return config.max_call_seconds
