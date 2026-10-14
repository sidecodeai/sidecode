"""The ``realtime`` speech backend: a live session over the OpenAI Realtime protocol."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import ServerConnection, serve

from rc_gateway import stt_realtime
from rc_gateway.app import build_state, create_app
from rc_gateway.config import ConfigError, SttConfig, load_config
from rc_gateway.stt import SttError, wav_from_pcm16
from rc_gateway.stt_realtime import LiveTranscription, RealtimeTranscriber, join_segments

from .conftest import FakePolisher, FakeWebPushSender, drain_until, make_config

SILENCE = b"\x00\x00" * 1600  # 100 ms of 16 kHz mono PCM16


class FakeRealtimeServer:
    """A vendor that speaks the documented Alibaba dialect, run on a loop of its own.

    Every ``input_audio_buffer.append`` is answered with a partial naming the bytes heard so
    far; ``input_audio_buffer.commit`` completes the sentence when audio is pending and, as
    Alibaba does, answers an ``error`` when nothing is; ``session.finish`` finishes the session.
    With ``vad_after`` the server completes the sentence itself after that many frames, the way
    server VAD does at a pause. With ``fail`` it answers the first frame with an ``error`` instead.
    With ``language`` it names that language on every partial and completion, as Alibaba does;
    without, it names none, as OpenAI's events do.
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        vad_after: int = 0,
        commit_errors: bool = False,
        language: str | None = None,
    ) -> None:
        self.fail = fail
        self.vad_after = vad_after
        # A vendor that refuses every commit, whatever the buffer holds.
        self.commit_errors = commit_errors
        self.language = language
        self.requests: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.path = ""
        self.port = 0
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api-ws/v1/realtime?model=qwen3-asr-flash-realtime"

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(5), "the fake vendor did not start"

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)

    def _run(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._handle, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop.wait()

    async def _handle(self, connection: ServerConnection) -> None:
        request = connection.request
        assert request is not None
        self.headers = {key.lower(): value for key, value in request.headers.items()}
        self.path = request.path
        heard = 0
        pending = 0
        frames = 0
        await connection.send(json.dumps({"type": "session.created", "session": {"id": "s1"}}))
        async for raw in connection:
            event = json.loads(raw)
            self.requests.append(event)
            kind = event["type"]
            if kind == "session.update":
                await connection.send(json.dumps({"type": "session.updated"}))
            elif kind == "input_audio_buffer.append":
                if self.fail:
                    await connection.send(
                        json.dumps({"type": "error", "error": {"message": "quota exhausted"}})
                    )
                    continue
                size = len(base64.b64decode(event["audio"]))
                heard += size
                pending += size
                frames += 1
                await connection.send(
                    self._named(
                        {
                            "type": "conversation.item.input_audio_transcription.text",
                            "text": f"heard {heard}",
                            "stash": " bytes",
                        }
                    )
                )
                if self.vad_after and frames % self.vad_after == 0:
                    await connection.send(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
                    await connection.send(self._named(_completed(heard)))
                    pending = 0
            elif kind == "input_audio_buffer.commit":
                if pending == 0 or self.commit_errors:
                    await connection.send(
                        json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "message": "Error committing input audio buffer, "
                                    "maybe no invalid audio stream."
                                },
                            }
                        )
                    )
                    continue
                pending = 0
                await connection.send(self._named(_completed(heard)))
            elif kind == "session.finish":
                await connection.send(json.dumps({"type": "session.finished"}))

    def _named(self, event: dict[str, Any]) -> str:
        """A transcription event; one naming a language carries an emotion too, as Alibaba's do."""
        if self.language is not None:
            event = {**event, "language": self.language, "emotion": "neutral"}
        return json.dumps(event)


def _completed(heard: int) -> dict[str, Any]:
    return {
        "type": "conversation.item.input_audio_transcription.completed",
        "transcript": f"final {heard} bytes.",
    }


@pytest.fixture
def vad_vendor() -> Iterator[FakeRealtimeServer]:
    server = FakeRealtimeServer(vad_after=2)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def vendor() -> Iterator[FakeRealtimeServer]:
    server = FakeRealtimeServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def failing_vendor() -> Iterator[FakeRealtimeServer]:
    server = FakeRealtimeServer(fail=True)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def naming_vendor() -> Iterator[FakeRealtimeServer]:
    server = FakeRealtimeServer(language="en")
    server.start()
    yield server
    server.stop()


def realtime_config(url: str) -> SttConfig:
    return SttConfig(
        provider="realtime",
        base_url="",
        api_key="sk-live",
        model="qwen3-asr-flash-realtime",
        realtime_url=url,
    )


def realtime_app(tmp_path: Path, url: str) -> TestClient:
    """A gateway whose speech provider is the vendor at ``url``."""
    config = make_config(tmp_path, stt=realtime_config(url))
    state = build_state(
        config,
        transcriber=RealtimeTranscriber(config.stt),
        polisher=FakePolisher(),
        web_sender=FakeWebPushSender(),
    )
    return TestClient(create_app(state))


@pytest.fixture
def realtime_client(tmp_path: Path, vendor: FakeRealtimeServer) -> Iterator[TestClient]:
    with realtime_app(tmp_path, vendor.url) as client:
        yield client


# ------------------------------------------------------------------- the session


async def test_a_live_session_streams_partials_and_returns_the_final(
    vendor: FakeRealtimeServer,
) -> None:
    partials: list[str] = []

    async def on_partial(text: str) -> None:
        partials.append(text)

    session = LiveTranscription(vendor.url, "sk-live", on_partial)
    await session.open()
    try:
        await session.append(SILENCE)
        await session.append(SILENCE)
        text = await session.finish()
    finally:
        await session.close()

    assert text == f"final {2 * len(SILENCE)} bytes."
    # Partials carry the tentative tail too, and the final replaces them.
    assert partials[0] == f"heard {len(SILENCE)} bytes"
    assert partials[-1] == text
    # This vendor names no language, as OpenAI's events do.
    assert session.language == "auto"
    assert vendor.headers["authorization"] == "Bearer sk-live"
    assert vendor.headers["openai-beta"] == "realtime=v1"
    assert vendor.path.endswith("?model=qwen3-asr-flash-realtime")
    update = vendor.requests[0]
    assert update["type"] == "session.update"
    assert update["session"]["input_audio_format"] == "pcm"
    assert update["session"]["sample_rate"] == 16000
    assert update["session"]["turn_detection"]["type"] == "server_vad"
    # A44: the vendor detects the language, so the session asks for none.
    assert "input_audio_transcription" not in update["session"]
    assert [event["type"] for event in vendor.requests[-2:]] == [
        "input_audio_buffer.commit",
        "session.finish",
    ]


async def test_a_sentence_the_vendor_already_completed_is_not_committed_again(
    vad_vendor: FakeRealtimeServer,
) -> None:
    """Server VAD had committed the sentence at the pause; a second commit met an
    empty buffer and the vendor answered with an error."""

    async def ignore(_: str) -> None:
        return None

    session = LiveTranscription(vad_vendor.url, "sk", ignore)
    await session.open()
    try:
        await session.append(SILENCE)
        await session.append(SILENCE)
        # Let the vendor's own completion land before Done is pressed.
        for _ in range(50):
            if session.text.startswith("final"):
                break
            await asyncio.sleep(0.02)
        text = await session.finish()
    finally:
        await session.close()
    assert text == f"final {2 * len(SILENCE)} bytes."
    assert "input_audio_buffer.commit" not in [event["type"] for event in vad_vendor.requests]


async def test_a_complaint_while_finishing_does_not_lose_the_words() -> None:
    """A vendor that refuses the commit on Done: the words heard so far are still the answer,
    and nothing reaches the app as an error."""
    server = FakeRealtimeServer(commit_errors=True)
    server.start()
    errors: list[str] = []

    async def ignore(_: str) -> None:
        return None

    async def on_error(error: SttError) -> None:
        errors.append(str(error))

    session = LiveTranscription(server.url, "sk", ignore, on_error)
    try:
        await session.open()
        await session.append(SILENCE)
        for _ in range(50):
            if session.text:
                break
            await asyncio.sleep(0.02)
        text = await session.finish()
    finally:
        await session.close()
        server.stop()
    assert text == f"heard {len(SILENCE)} bytes"
    assert errors == []
    assert [event["type"] for event in server.requests[-2:]] == [
        "input_audio_buffer.commit",
        "session.finish",
    ]


async def test_the_language_the_vendor_names_is_the_utterances(
    naming_vendor: FakeRealtimeServer,
) -> None:
    """A44: nobody tells the vendor a language; the one Alibaba says it heard is reported."""

    async def ignore(_: str) -> None:
        return None

    session = LiveTranscription(naming_vendor.url, "", ignore)
    assert session.language == "auto"
    await session.open()
    try:
        await session.append(SILENCE)
        await session.finish()
    finally:
        await session.close()
    assert session.language == "en"
    assert "input_audio_transcription" not in naming_vendor.requests[0]["session"]
    assert "authorization" not in naming_vendor.headers


async def test_the_last_language_the_vendor_named_wins() -> None:
    """Alibaba names one per sentence; an event that names none, or nothing usable, keeps it."""

    async def ignore(_: str) -> None:
        return None

    session = LiveTranscription("ws://127.0.0.1:9/realtime", "", ignore)
    completed = stt_realtime.COMPLETED_EVENT
    await session._handle({"type": completed, "transcript": "先跑测试。", "language": "zh"})
    await session._handle({"type": completed, "transcript": "Then ship it.", "language": "en"})
    await session._handle({"type": stt_realtime.DELTA_EVENT, "delta": "and"})
    await session._handle({"type": stt_realtime.PARTIAL_EVENT, "text": "then", "language": ""})
    assert session.language == "en"


async def test_a_vendor_error_surfaces_as_an_stt_error(failing_vendor: FakeRealtimeServer) -> None:
    async def ignore(_: str) -> None:
        return None

    session = LiveTranscription(failing_vendor.url, "sk", ignore)
    await session.open()
    try:
        await session.append(SILENCE)
        with pytest.raises(SttError, match="quota exhausted"):
            for _ in range(50):
                await asyncio.sleep(0.02)
                await session.append(SILENCE)
    finally:
        await session.close()


async def test_an_unreachable_vendor_is_an_stt_error() -> None:
    async def ignore(_: str) -> None:
        return None

    session = LiveTranscription("ws://127.0.0.1:9/realtime", "sk", ignore)
    with pytest.raises(SttError, match="unreachable"):
        await session.open()


def test_segments_join_without_spaces_between_chinese_and_with_them_between_english() -> None:
    assert join_segments(["今天天气很好。", "我们去公园吧"]) == "今天天气很好。我们去公园吧"
    assert join_segments(["Hello there.", "How are you"]) == "Hello there. How are you"
    assert join_segments(["打开 VS Code", "然后运行测试"]) == "打开 VS Code然后运行测试"
    assert join_segments(["", "  ", "one"]) == "one"


# ---------------------------------------------------------------- through the app


def test_the_live_socket_streams_words_and_ends_on_stop(
    realtime_client: TestClient, auth: dict[str, str], vendor: FakeRealtimeServer
) -> None:
    # An older app still names a language (A44): ignored, and a vendor that names none is `auto`.
    with realtime_client.websocket_connect("/ws/stt?language=zh", headers=auth) as socket:
        socket.send_bytes(SILENCE)
        first = socket.receive_json()
        socket.send_bytes(SILENCE)
        second = socket.receive_json()
        socket.send_json({"type": "stt.stop"})
        frames = [socket.receive_json()]
        while frames[-1]["type"] != "stt.final":
            frames.append(socket.receive_json())
    assert first == {"type": "stt.partial", "text": f"heard {len(SILENCE)} bytes"}
    assert second == {"type": "stt.partial", "text": f"heard {2 * len(SILENCE)} bytes"}
    assert frames[-1] == {
        "type": "stt.final",
        "text": f"final {2 * len(SILENCE)} bytes.",
        "language": "auto",
    }
    assert "input_audio_transcription" not in vendor.requests[0]["session"]


def test_the_language_the_vendor_heard_reaches_the_app(
    tmp_path: Path, naming_vendor: FakeRealtimeServer, auth: dict[str, str]
) -> None:
    """A44: whatever an older app asks for, the answer names the language the vendor heard."""
    with realtime_app(tmp_path, naming_vendor.url) as client:
        with client.websocket_connect("/ws/stt?language=zh", headers=auth) as socket:
            socket.send_bytes(SILENCE)
            socket.send_json({"type": "stt.stop"})
            final = drain_until(socket, "stt.final")
        uploaded = client.post(
            "/api/stt/transcribe",
            files={"audio": ("clip.wav", wav_from_pcm16(SILENCE), "audio/wav")},
            data={"language": "zh"},
            headers=auth,
        )
    assert final["language"] == "en"
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["language"] == "en"
    updates = [event for event in naming_vendor.requests if event["type"] == "session.update"]
    assert len(updates) == 2
    assert all("input_audio_transcription" not in event["session"] for event in updates)


def test_a_stop_before_any_audio_is_an_empty_final(
    realtime_client: TestClient, auth: dict[str, str]
) -> None:
    with realtime_client.websocket_connect("/ws/stt", headers=auth) as socket:
        socket.send_json({"type": "stt.stop"})
        final = socket.receive_json()
    assert final == {"type": "stt.final", "text": "", "language": "auto"}


def test_the_live_socket_reports_the_vendor_refusing(
    tmp_path: Path, failing_vendor: FakeRealtimeServer, auth: dict[str, str]
) -> None:
    with (
        realtime_app(tmp_path, failing_vendor.url) as client,
        client.websocket_connect("/ws/stt", headers=auth) as socket,
    ):
        frame: dict[str, Any] = {}
        for _ in range(50):
            socket.send_bytes(SILENCE)
            try:
                frame = socket.receive_json()
            except Exception:
                break
            if frame.get("type") == "stt.error":
                break
    assert frame["type"] == "stt.error"
    assert "quota exhausted" in frame["message"]


def test_a_recorded_wav_is_played_into_a_live_session(
    realtime_client: TestClient, auth: dict[str, str]
) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(16000)
        sink.writeframes(SILENCE * 3)
    response = realtime_client.post(
        "/api/stt/transcribe",
        files={"audio": ("clip.wav", buffer.getvalue(), "audio/wav")},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"text": f"final {3 * len(SILENCE)} bytes.", "language": "auto"}


def test_a_compressed_upload_is_refused_by_the_live_backend(
    realtime_client: TestClient, auth: dict[str, str]
) -> None:
    response = realtime_client.post(
        "/api/stt/transcribe",
        files={"audio": ("clip.m4a", b"\x00\x00\x00\x18ftypM4A ", "audio/mp4")},
        headers=auth,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_a_wav_at_another_rate_is_refused() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(44100)
        sink.writeframes(SILENCE)
    with pytest.raises(SttError, match="16 kHz"):
        stt_realtime.pcm_from_upload(buffer.getvalue(), "audio/wav")


# ------------------------------------------------------------------- the config


def test_the_realtime_provider_needs_a_socket_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)
    monkeypatch.setenv("STT_PROVIDER", "realtime")
    monkeypatch.delenv("STT_REALTIME_URL", raising=False)
    with pytest.raises(ConfigError, match="STT_REALTIME_URL"):
        load_config(load_env_file=False)


def test_the_realtime_provider_carries_the_model_in_its_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)
    monkeypatch.setenv("STT_PROVIDER", "realtime")
    monkeypatch.setenv("STT_MODEL", "qwen3-asr-flash-realtime")
    monkeypatch.setenv(
        "STT_REALTIME_URL", "wss://ws.example.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
    )
    config = load_config(load_env_file=False)
    assert config.stt.enabled
    assert config.stt.realtime_url.endswith("/api-ws/v1/realtime?model=qwen3-asr-flash-realtime")
    monkeypatch.setenv("STT_REALTIME_URL", "wss://ws.example/api-ws/v1/realtime?model=other")
    assert load_config(load_env_file=False).stt.realtime_url.endswith("?model=other")
