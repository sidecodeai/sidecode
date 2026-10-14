"""``WS /ws/stt`` — live transcription of PCM16 audio.

Binary frames carry raw little-endian 16-bit PCM at 16 kHz mono. With a request/response backend
the gateway wraps the accumulated buffer as WAV and asks for a transcript roughly every two seconds
while audio keeps arriving, skipping a partial whenever a request is still in flight, and produces
the final transcript on ``stt.stop``. With the ``realtime`` backend (``stt_realtime.py``) each frame
is forwarded the moment it arrives and every incremental word the vendor sends comes back as
``stt.partial``; ``stt.stop`` asks for the last sentence. Audio is never written to disk.

The provider detects the language (A44). A ``language`` query parameter an older app sends is
ignored, and ``stt.final`` carries the language the provider reported, or ``auto``.

Every transcription spends the operator's speech credit, so the socket is bounded the way nothing
else on this path was: the address is rate limited at the upgrade, an account may hold only a few
streams at once, a stream that goes quiet is closed, and signing out closes it immediately rather
than letting it transcribe on the revoked session's behalf.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..frames import SILENT_TIMEOUT_SECONDS
from ..logging import logger
from ..security import (
    Credential,
    accept_for_close,
    authenticate_websocket,
    client_ip,
    state_of,
)
from ..state import GatewayState
from ..stt import PARTIAL_INTERVAL_SECONDS, SttError, Transcriber, Utterance
from ..stt_realtime import LiveTranscription, RealtimeTranscriber

log = logger("rc_gateway.ws.stt")
router = APIRouter()

#: Live streams one account may hold. A person dictates into one composer at a time; four covers
#: a phone and a browser with one stale socket each.
MAX_SOCKETS_PER_USER = 4


@router.websocket("/ws/stt")
async def stt_socket(ws: WebSocket) -> None:
    state = state_of(ws)
    if state.stt_limiter.limited(client_ip(ws, state)):
        log.warning("stt upgrade rate limited")
        if await accept_for_close(ws):
            await _fail(ws, "too many speech requests", "too_many_requests")
        return
    credential = await authenticate_websocket(ws, state.stt_rejects)
    if credential is None:
        return
    if state.transcriber is None:
        await _fail(ws, "speech-to-text is not configured", "unsupported")
        return
    if state.stt_sockets[credential.username] >= MAX_SOCKETS_PER_USER:
        log.warning("stt socket refused: account at the stream cap")
        await _fail(ws, "too many speech streams open", "too_many_requests")
        return

    state.stt_sockets[credential.username] += 1
    session = _Stream(ws, state.transcriber)
    watchdog = asyncio.create_task(_watch_revocation(session, state, credential))
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("stt socket failed")
    finally:
        watchdog.cancel()
        _release_socket(state, credential.username)
        await session.close()


def _release_socket(state: GatewayState, username: str) -> None:
    state.stt_sockets[username] -= 1
    if state.stt_sockets[username] <= 0:
        del state.stt_sockets[username]


async def _watch_revocation(session: _Stream, state: GatewayState, credential: Credential) -> None:
    """Close the stream the moment its login session is signed out or the account is disabled.

    ``/ws/app`` has had this since A24; without it here a socket open at the moment of
    ``POST /api/logout`` keeps transcribing the user's audio and keeps spending speech credit.
    """
    event = await state.sessions.revoked_event(credential.claims)
    if event is not None:
        try:
            await event.wait()
        except asyncio.CancelledError:
            return
    log.info("closing stt socket for a revoked session")
    await session.abort("session revoked", "unauthorized")


class _Stream:
    def __init__(self, ws: WebSocket, transcriber: Transcriber) -> None:
        self.ws = ws
        self.transcriber = transcriber
        self.utterance = Utterance()
        # The realtime backend streams; the others answer whole utterances (see the module doc).
        self.realtime = transcriber if isinstance(transcriber, RealtimeTranscriber) else None
        self.live: LiveTranscription | None = None
        self._partial: asyncio.Task[None] | None = None
        # The first partial is due one interval in, not on the first 100 ms chunk: transcribing
        # a fifth of a second costs a backend round trip and tells the user nothing.
        self._last_partial = time.monotonic()
        self._closed = False

    async def run(self) -> None:
        while True:
            # Nothing else would ever close a silent stream: the hub's heartbeat walks only
            # devices and apps, and uvicorn's own WebSocket ping is disabled deliberately.
            try:
                message = await asyncio.wait_for(self.ws.receive(), timeout=SILENT_TIMEOUT_SECONDS)
            except TimeoutError:
                log.info("closing silent stt socket")
                await self.abort("no audio received", "timeout")
                return
            kind = message.get("type")
            if kind == "websocket.disconnect":
                return
            if (payload := message.get("bytes")) is not None:
                if not await self._append(payload):
                    return
                if self.realtime is not None:
                    if not await self._live_append(payload):
                        return
                else:
                    await self._maybe_partial()
                continue
            text = message.get("text")
            if text is None:
                continue
            command = _command(text)
            if command == "stt.stop":
                await self._finalize()
                return
            if command == "stt.cancel":
                return

    async def abort(self, message: str, code: str) -> None:
        """Stop transcribing and close, whatever the reader is doing."""
        if self._closed:
            return
        self._closed = True
        await self._cancel_partial()
        await self._close_live()
        await _fail(self.ws, message, code)

    async def _append(self, payload: bytes) -> bool:
        try:
            self.utterance.append(payload)
        except SttError as exc:
            await _fail(self.ws, str(exc), exc.code)
            return False
        return True

    async def _live_append(self, payload: bytes) -> bool:
        """Forward one frame to the live session, opening it on the first."""
        assert self.realtime is not None
        try:
            if self.live is None:
                self.live = self.realtime.live(self._live_partial, self._live_error)
                await self.live.open()
            await self.live.append(payload)
        except SttError as exc:
            await _fail(self.ws, str(exc), exc.code)
            return False
        return True

    async def _live_partial(self, text: str) -> None:
        if not self._closed and text:
            await _send(self.ws, {"type": "stt.partial", "text": text})

    async def _live_error(self, error: SttError) -> None:
        """The vendor refused or dropped the stream: tell the app now, not on its next frame."""
        if self._closed:
            return
        self._closed = True
        await _fail(self.ws, str(error), error.code)

    async def _live_finalize(self) -> None:
        live = self.live
        if live is None:
            await _send(self.ws, {"type": "stt.final", "text": "", "language": "auto"})
            await _close(self.ws)
            return
        try:
            text = await live.finish()
        except SttError as exc:
            await _fail(self.ws, str(exc), exc.code)
            return
        finally:
            await self._close_live()
        await _send(self.ws, {"type": "stt.final", "text": text, "language": live.language})
        await _close(self.ws)

    async def _close_live(self) -> None:
        live, self.live = self.live, None
        if live is not None:
            await live.close()

    async def _maybe_partial(self) -> None:
        busy = self._partial is not None and not self._partial.done()
        if busy or time.monotonic() - self._last_partial < PARTIAL_INTERVAL_SECONDS:
            return
        self._last_partial = time.monotonic()
        self._partial = asyncio.create_task(self._send_partial(self.utterance.wav()))

    async def _send_partial(self, wav: bytes) -> None:
        try:
            transcript = await self.transcriber.transcribe(
                wav, filename="audio.wav", content_type="audio/wav"
            )
        except SttError:
            return
        if not self._closed and transcript.text:
            await _send(self.ws, {"type": "stt.partial", "text": transcript.text})

    async def _finalize(self) -> None:
        if self.realtime is not None:
            await self._live_finalize()
            return
        await self._cancel_partial()
        if self.utterance.size == 0:
            await _send(self.ws, {"type": "stt.final", "text": "", "language": "auto"})
            await _close(self.ws)
            return
        try:
            transcript = await self.transcriber.transcribe(
                self.utterance.wav(), filename="audio.wav", content_type="audio/wav"
            )
        except SttError as exc:
            await _fail(self.ws, str(exc), exc.code)
            return
        await _send(
            self.ws,
            {"type": "stt.final", "text": transcript.text, "language": transcript.language},
        )
        await _close(self.ws)

    async def _cancel_partial(self) -> None:
        if self._partial is not None and not self._partial.done():
            self._partial.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._partial
        self._partial = None

    async def close(self) -> None:
        self._closed = True
        await self._cancel_partial()
        await self._close_live()


def _command(text: str) -> str:
    try:
        frame = json.loads(text)
    except ValueError:
        return ""
    return str(frame.get("type", "")) if isinstance(frame, dict) else ""


async def _send(ws: WebSocket, frame: dict[str, Any]) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await ws.send_text(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))


async def _fail(ws: WebSocket, message: str, code: str = "internal") -> None:
    await _send(ws, {"type": "stt.error", "message": message, "code": code})
    await _close(ws)


async def _close(ws: WebSocket) -> None:
    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
        await ws.close()
