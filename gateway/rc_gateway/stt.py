"""Speech-to-text through a hosted provider.

Two backends speak to the same ``Transcriber`` protocol: an OpenAI-compatible
``/audio/transcriptions`` endpoint, and Xiaomi MiMo, which has no transcription endpoint and
recognises audio through a chat completion instead.

The gateway never stores audio: a request's bytes live in memory for the length of one HTTP call.
Streaming transcription accumulates raw PCM and re-transcribes the whole utterance every couple of
seconds, which is what a whisper-style backend supports without a realtime protocol.

No request names a language: the provider detects it (A44), so there is nothing to choose and
nothing an app sends is forwarded.
"""

from __future__ import annotations

import base64
import io
import struct
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import SttConfig
from .logging import logger

log = logger("rc_gateway.stt")

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
MAX_UTTERANCE_SECONDS = 120
MAX_UTTERANCE_BYTES = 4 * 1024 * 1024
PARTIAL_INTERVAL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 20.0
#: MiMo refuses an ``input_audio`` data URL whose base64 payload is larger than this.
MAX_BASE64_BYTES = 10 * 1024 * 1024


class SttError(RuntimeError):
    def __init__(self, message: str, code: str = "internal") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Transcript:
    text: str
    #: The language the provider reported, or ``auto`` when it names none (A44).
    language: str


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> Transcript: ...


def _unreachable(exc: httpx.HTTPError) -> SttError:
    log.warning("stt backend unreachable", error=type(exc).__name__)
    return SttError("speech-to-text backend is unreachable")


def _decoded(response: httpx.Response) -> Any:
    """Return the decoded body, or raise the ``SttError`` the caller should surface."""
    if response.status_code >= 400:
        log.warning("stt backend rejected the request", status=response.status_code)
        raise SttError(f"speech-to-text backend returned {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise SttError("speech-to-text backend returned a non-JSON body") from exc


class _HttpTranscriber:
    """The client lifecycle and authorization header both hosted providers share."""

    def __init__(self, config: SttConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


class OpenAiTranscriber(_HttpTranscriber):
    """Posts multipart audio to ``{base_url}/audio/transcriptions``.

    OpenAI's default ``json`` answer carries the text alone; a compatible server that also names
    the language it detected is believed.
    """

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> Transcript:
        if not audio:
            raise SttError("no audio received", "bad_request")
        try:
            response = await self._http().post(
                f"{self.config.base_url}/audio/transcriptions",
                files={"file": (filename, audio, content_type)},
                data={"model": self.config.model},
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        payload = _decoded(response)
        text = payload.get("text") if isinstance(payload, dict) else None
        detected = payload.get("language") if isinstance(payload, dict) else None
        return Transcript(
            text=text.strip() if isinstance(text, str) else "",
            language=detected if isinstance(detected, str) and detected else "auto",
        )


class MimoTranscriber(_HttpTranscriber):
    """Posts audio to MiMo's ``{base_url}/chat/completions``, which answers with the transcript.

    MiMo has no transcription endpoint: the audio travels as a base64 data URL in the only user
    message and the transcript comes back as the assistant's content. With no ``asr_options``
    beside ``messages`` MiMo detects the language itself, and its answer does not say which.
    """

    async def transcribe(self, audio: bytes, *, filename: str, content_type: str) -> Transcript:
        if not audio:
            raise SttError("no audio received", "bad_request")
        encoded = base64.b64encode(audio).decode("ascii")
        if len(encoded) > MAX_BASE64_BYTES:
            raise SttError("audio exceeds the backend's 10 MB base64 limit", "too_large")
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:{content_type};base64,{encoded}"},
                        }
                    ],
                }
            ],
        }
        try:
            response = await self._http().post(
                f"{self.config.base_url}/chat/completions",
                json=body,
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from exc
        return Transcript(text=_completion_text(_decoded(response)), language="auto")


def _completion_text(payload: Any) -> str:
    """Read ``choices[0].message.content`` out of a chat completion."""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return content.strip() if isinstance(content, str) else ""


def wav_from_pcm16(
    pcm: bytes, *, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS
) -> bytes:
    """Wrap raw little-endian 16-bit PCM in a RIFF/WAVE container."""
    byte_rate = sample_rate * channels * BYTES_PER_SAMPLE
    block_align = channels * BYTES_PER_SAMPLE
    buffer = io.BytesIO()
    buffer.write(b"RIFF")
    buffer.write(struct.pack("<I", 36 + len(pcm)))
    buffer.write(b"WAVEfmt ")
    buffer.write(struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 16))
    buffer.write(b"data")
    buffer.write(struct.pack("<I", len(pcm)))
    buffer.write(pcm)
    return buffer.getvalue()


def pcm_duration_seconds(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)


class Utterance:
    """Accumulates PCM frames under the protocol's 120 s / 4 MiB limits."""

    def __init__(
        self,
        *,
        max_seconds: int = MAX_UTTERANCE_SECONDS,
        max_bytes: int = MAX_UTTERANCE_BYTES,
    ) -> None:
        self.max_seconds = max_seconds
        self.max_bytes = max_bytes
        self._chunks: list[bytes] = []
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    @property
    def duration(self) -> float:
        return self._size / (SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)

    def append(self, chunk: bytes) -> None:
        if self._size + len(chunk) > self.max_bytes:
            raise SttError("utterance exceeds the 4 MiB limit", "too_large")
        self._chunks.append(chunk)
        self._size += len(chunk)
        if self.duration > self.max_seconds:
            raise SttError("utterance exceeds the 120 s limit", "too_large")

    def wav(self) -> bytes:
        return wav_from_pcm16(b"".join(self._chunks))
