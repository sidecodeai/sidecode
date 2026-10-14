"""Live transcription over the OpenAI Realtime protocol: words as they are said.

The other backends answer a whole utterance; the gateway fakes partials by re-sending everything
every couple of seconds (``stt.py``). This one keeps a WebSocket open to the vendor for the life of
the utterance, forwards each PCM frame as ``input_audio_buffer.append`` and turns the vendor's
incremental events into ``stt.partial`` the moment they arrive. The dialect is the one Alibaba
Model Studio documents for ``qwen3-asr-flash-realtime`` — ``session.update`` with
``input_audio_format: pcm`` at 16 kHz, server VAD, partials as
``conversation.item.input_audio_transcription.text`` (``text`` plus a tentative ``stash``), finals
as ``….completed`` (``transcript``), and ``session.finish``/``session.finished`` to end — with
OpenAI's own ``….delta`` accepted as well, since the event names are shared.

The session names no language: the vendor detects it (A44). Alibaba says which one it heard on
every ``….text`` and ``….completed`` event, and the last one it named is the utterance's; OpenAI's
events name none, which reads as ``auto``.

Audio is never written to disk here either; it is base64 in flight and nothing more.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import time
import wave
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect

from .config import SttConfig
from .logging import logger
from .stt import BYTES_PER_SAMPLE, CHANNELS, SAMPLE_RATE, SttError, Transcript

log = logger("rc_gateway.stt.realtime")

OPEN_TIMEOUT_SECONDS = 10.0
#: How long the final transcript may take after the last audio: the vendor's VAD has to notice
#: the silence and its model has to finish the sentence.
FINISH_TIMEOUT_SECONDS = 8.0
#: The pause the vendor's VAD treats as the end of a sentence. Shorter cuts a speaker mid-thought,
#: longer holds the final back; 500 ms is what a dictated sentence's own pauses stay under.
SILENCE_DURATION_MS = 500
#: A file is fed in the frames the microphone would have sent.
FILE_CHUNK_BYTES = 3200

PARTIAL_EVENT = "conversation.item.input_audio_transcription.text"
DELTA_EVENT = "conversation.item.input_audio_transcription.delta"
COMPLETED_EVENT = "conversation.item.input_audio_transcription.completed"
#: The events on which Alibaba names the language it heard.
LANGUAGE_EVENTS = (PARTIAL_EVENT, COMPLETED_EVENT)

OnPartial = Callable[[str], Awaitable[None]]
OnError = Callable[[SttError], Awaitable[None]]


def join_segments(segments: list[str]) -> str:
    """Sentences the vendor completed one after another, read as one text.

    Chinese runs on without spaces and English does not, so a boundary gets a space only when
    both sides are non-CJK; a segment's own punctuation carries it.
    """
    text = ""
    for segment in segments:
        piece = segment.strip()
        if not piece:
            continue
        if text and not (_is_cjk(text[-1]) or _is_cjk(piece[0])):
            text += " "
        text += piece
    return text


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        0x3000 <= code <= 0x303F  # CJK punctuation
        or 0x3400 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF  # full-width forms
        or 0x20000 <= code <= 0x2FFFF
    )


class LiveTranscription:
    """One utterance on the vendor's socket: audio in, partials out, one final."""

    def __init__(
        self,
        url: str,
        api_key: str,
        on_partial: OnPartial,
        on_error: OnError | None = None,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.on_partial = on_partial
        # Told the moment the vendor refuses or drops the stream, so the person is not left
        # dictating into a socket that will only fail on their next frame.
        self.on_error = on_error
        self._socket: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._segments: list[str] = []
        self._pending = ""
        self._language: str | None = None
        self._finished = asyncio.Event()
        self._finishing = False
        # Audio sent since the vendor last closed a sentence: what a commit on Done is for.
        self._fresh_audio = False
        self._error: SttError | None = None

    async def open(self) -> None:
        headers = {"OpenAI-Beta": "realtime=v1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            self._socket = await connect(
                self.url, additional_headers=headers, open_timeout=OPEN_TIMEOUT_SECONDS
            )
        except (OSError, websockets.exceptions.WebSocketException, TimeoutError) as exc:
            log.warning("realtime stt backend unreachable", error=type(exc).__name__)
            raise SttError("speech-to-text backend is unreachable") from exc
        await self._send(self._session_update())
        self._reader = asyncio.create_task(self._read())

    def _session_update(self) -> dict[str, Any]:
        session: dict[str, Any] = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": SAMPLE_RATE,
            "turn_detection": {"type": "server_vad", "silence_duration_ms": SILENCE_DURATION_MS},
        }
        return {"event_id": _event_id(), "type": "session.update", "session": session}

    async def append(self, pcm: bytes) -> None:
        self._raise_if_failed()
        self._fresh_audio = True
        await self._send(
            {
                "event_id": _event_id(),
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }
        )

    async def finish(self) -> str:
        """Ask for the last sentence, if one is open, and return everything heard.

        With server VAD the vendor commits each sentence itself once the speaker pauses, so a
        commit sent after that meets an empty buffer and Alibaba answers it with an error ("Error
        committing input audio buffer, maybe no invalid audio stream"). A commit therefore goes out
        only while a sentence is still in progress, and
        whatever the vendor says while the session is being finished cannot fail the utterance:
        the words heard so far are the result.
        """
        self._raise_if_failed()
        self._finishing = True
        if self._pending or self._fresh_audio:
            await self._send({"event_id": _event_id(), "type": "input_audio_buffer.commit"})
        await self._send({"event_id": _event_id(), "type": "session.finish"})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._finished.wait(), FINISH_TIMEOUT_SECONDS)
        return self.text

    @property
    def text(self) -> str:
        """What has been heard so far: the finished sentences, then the one in progress."""
        return join_segments([*self._segments, self._pending])

    @property
    def language(self) -> str:
        """The language the vendor last said it heard, or ``auto`` while it has named none."""
        return self._language or "auto"

    async def close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        socket, self._socket = self._socket, None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()

    # ------------------------------------------------------------------ wire

    async def _send(self, frame: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise SttError("speech-to-text backend is unreachable")
        try:
            await socket.send(json.dumps(frame, separators=(",", ":")))
        except websockets.exceptions.WebSocketException as exc:
            raise SttError("speech-to-text backend is unreachable") from exc

    async def _read(self) -> None:
        socket = self._socket
        if socket is None:
            return
        try:
            async for raw in socket:
                event = _decode(raw)
                if event is not None:
                    await self._handle(event)
        except websockets.exceptions.ConnectionClosedOK:
            pass
        except websockets.exceptions.WebSocketException as exc:
            await self._fail(SttError("speech-to-text backend closed the stream"), exc)
        finally:
            self._finished.set()

    async def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in LANGUAGE_EVENTS:
            self._note_language(event.get("language"))
        if kind == PARTIAL_EVENT:
            # `stash` is the tail the vendor may still revise; it reads better than nothing.
            self._pending = f"{event.get('text') or ''}{event.get('stash') or ''}"
            await self.on_partial(self.text)
        elif kind == DELTA_EVENT:
            self._pending += str(event.get("delta") or "")
            await self.on_partial(self.text)
        elif kind == COMPLETED_EVENT:
            transcript = str(event.get("transcript") or "").strip()
            if transcript:
                self._segments.append(transcript)
            self._pending = ""
            self._fresh_audio = False
            await self.on_partial(self.text)
        elif kind == "input_audio_buffer.speech_stopped":
            # The vendor's VAD closed the sentence; its own commit follows.
            self._fresh_audio = False
        elif kind == "session.finished":
            self._finished.set()
        elif kind == "error":
            raw_error = event.get("error")
            detail: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            message = str(detail.get("message") or event.get("message") or "backend error")
            if self._finishing:
                # Nothing more was going to be heard anyway; the text so far is the answer.
                log.info("realtime stt: the vendor complained while finishing", said=message[:200])
                self._finished.set()
                return
            await self._fail(SttError(f"speech-to-text backend refused: {message}"[:200]), None)

    def _note_language(self, value: Any) -> None:
        if isinstance(value, str) and value:
            self._language = value

    async def _fail(self, error: SttError, cause: BaseException | None) -> None:
        log.warning(
            "realtime stt failed", error=str(error), cause=type(cause).__name__ if cause else ""
        )
        first = self._error is None
        if first:
            self._error = error
        self._finished.set()
        if first and self.on_error is not None:
            await self.on_error(error)

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error


class RealtimeTranscriber:
    """The ``realtime`` provider: live sessions for ``/ws/stt``, and a file fed as if live."""

    def __init__(self, config: SttConfig) -> None:
        self.config = config

    def live(self, on_partial: OnPartial, on_error: OnError | None = None) -> LiveTranscription:
        return LiveTranscription(
            self.config.realtime_url, self.config.api_key, on_partial, on_error
        )

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> Transcript:
        """A recorded file, played into a live session frame by frame."""
        pcm = pcm_from_upload(audio, content_type)

        async def ignore(_: str) -> None:
            return None

        session = self.live(ignore)
        await session.open()
        try:
            for start in range(0, len(pcm), FILE_CHUNK_BYTES):
                await session.append(pcm[start : start + FILE_CHUNK_BYTES])
            text = await session.finish()
        finally:
            await session.close()
        return Transcript(text=text, language=session.language)


def pcm_from_upload(audio: bytes, content_type: str) -> bytes:
    """The 16 kHz mono PCM16 a live session takes, out of a WAV or raw PCM upload.

    The live protocol has no decoder behind it, so a compressed upload is refused rather than
    sent as noise; the apps record WAV or raw PCM, which is all this path has ever needed.
    """
    if content_type in ("audio/pcm", "audio/l16", "application/octet-stream"):
        return audio
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio)) as source:
                if (
                    source.getframerate() != SAMPLE_RATE
                    or source.getnchannels() != CHANNELS
                    or source.getsampwidth() != BYTES_PER_SAMPLE
                ):
                    raise SttError("the live backend takes 16 kHz mono PCM16 only", "bad_request")
                frames: bytes = source.readframes(source.getnframes())
                return frames
        except wave.Error as exc:
            raise SttError("the upload is not a readable WAV file", "bad_request") from exc
    raise SttError("the live backend takes WAV or raw PCM uploads only", "bad_request")


def _event_id() -> str:
    return f"event_{int(time.time() * 1000)}"


def _decode(raw: str | bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None
