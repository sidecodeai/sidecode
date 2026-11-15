"""Amendment A57: batch dictation.

`hello` and `GET /api/config` say whether the speech backend streams (`stt.streaming`). An app
dictating through one that does not opens `WS /ws/stt?partials=0`: no `stt.partial` comes back,
and each segment is transcribed once, on `stt.stop`. Without the parameter, or with any other
value, nothing changes, which every app released before A57 relies on.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.config import STT_PROVIDERS, SttConfig

from .conftest import FakeTranscriber, collect_until, drain_until, make_config, sign_in
from .test_stt_realtime import FakeRealtimeServer, realtime_app

SILENCE = b"\x00\x00" * 1600  # 100 ms of 16 kHz mono PCM16
WAV_HEADER_BYTES = 44
#: Long enough for the gateway to have handled every frame sent before it, and for any partial
#: those frames scheduled to have run.
SETTLE_SECONDS = 0.05
#: No parameter, as every app before A57 sends, and values that are not exactly `0`.
KEEPS_PARTIALS = ["", "?partials=1", "?partials=", "?partials=false", "?partials=00"]


@pytest.fixture(autouse=True)
def fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame the gateway never sends would hold a test for its 90 s silence limit; a second,
    well past ``SETTLE_SECONDS``, closes the socket with an error that fails the test at once."""
    monkeypatch.setattr("rc_gateway.ws.stt_ws.SILENT_TIMEOUT_SECONDS", 1.0)


@pytest.fixture
def due_on_every_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial is due on every frame, so a socket that asks for none is put to the test."""
    monkeypatch.setattr("rc_gateway.ws.stt_ws.PARTIAL_INTERVAL_SECONDS", 0.0)


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


# ----------------------------------------------------------------------- stt.streaming


def _stt_config(provider: str) -> SttConfig:
    return SttConfig(
        provider=provider,
        base_url="http://stt.invalid/v1",
        api_key="k",
        model="whisper-1",
        realtime_url="ws://stt.invalid/realtime" if provider == "realtime" else "",
    )


@pytest.mark.parametrize("provider", STT_PROVIDERS)
def test_config_and_hello_say_whether_the_backend_streams(tmp_path: Path, provider: str) -> None:
    """Only `realtime` streams, and the field is there for every provider, `none` included."""
    config = make_config(tmp_path, stt=_stt_config(provider))
    with TestClient(create_app(build_state(config))) as client:
        headers = {"Authorization": f"Bearer {sign_in(client, 'admin', config.password)}"}
        served = client.get("/api/config", headers=headers).json()["stt"]
        with client.websocket_connect("/ws/app", headers=headers) as app:
            hello = drain_until(app, "hello")["stt"]
    expected = {
        "enabled": provider != "none",
        "languages": ["auto"],
        "streaming": provider == "realtime",
    }
    assert served == expected
    assert hello == expected


# ------------------------------------------------------- a whole-recording backend


@pytest.mark.usefixtures("due_on_every_frame")
def test_without_partials_each_segment_is_transcribed_once_on_stop(
    client: TestClient, auth: dict[str, str], transcriber: FakeTranscriber
) -> None:
    """Two segments, two sockets: the final is the first frame either sends, and the one
    transcription of its segment, of all the audio, exactly as before."""
    for frames in (3, 5):
        with client.websocket_connect("/ws/stt?partials=0", headers=auth) as socket:
            for _ in range(frames):
                socket.send_bytes(SILENCE)
            time.sleep(SETTLE_SECONDS)
            socket.send_json({"type": "stt.stop"})
            answer = socket.receive_json()
        assert answer == {"type": "stt.final", "text": "hello world", "language": "en"}
    assert [call["bytes"] for call in transcriber.calls] == [
        WAV_HEADER_BYTES + 3 * len(SILENCE),
        WAV_HEADER_BYTES + 5 * len(SILENCE),
    ]


@pytest.mark.usefixtures("due_on_every_frame")
@pytest.mark.parametrize("query", KEEPS_PARTIALS)
def test_no_parameter_or_any_other_value_keeps_the_partials(
    client: TestClient, auth: dict[str, str], transcriber: FakeTranscriber, query: str
) -> None:
    with client.websocket_connect(f"/ws/stt{query}", headers=auth) as socket:
        socket.send_bytes(SILENCE)
        partial = socket.receive_json()
        socket.send_json({"type": "stt.stop"})
        final = drain_until(socket, "stt.final")
    assert partial == {"type": "stt.partial", "text": "hello world"}
    assert final == {"type": "stt.final", "text": "hello world", "language": "en"}
    assert len(transcriber.calls) == 2


def test_without_partials_a_stop_before_any_audio_is_still_an_empty_final(
    client: TestClient, auth: dict[str, str], transcriber: FakeTranscriber
) -> None:
    with client.websocket_connect("/ws/stt?partials=0", headers=auth) as socket:
        socket.send_json({"type": "stt.stop"})
        final = socket.receive_json()
    assert final == {"type": "stt.final", "text": "", "language": "auto"}
    assert transcriber.calls == []


def test_without_partials_a_backend_failure_is_still_an_stt_error(
    client: TestClient, auth: dict[str, str], transcriber: FakeTranscriber
) -> None:
    transcriber.fail = "backend down"
    with client.websocket_connect("/ws/stt?partials=0", headers=auth) as socket:
        socket.send_bytes(SILENCE)
        socket.send_json({"type": "stt.stop"})
        error = socket.receive_json()
    assert error == {"type": "stt.error", "message": "backend down", "code": "internal"}
    assert len(transcriber.calls) == 1


# ---------------------------------------------------------------- the realtime backend


def test_realtime_without_partials_forwards_the_final_alone(
    tmp_path: Path, vendor: FakeRealtimeServer, auth: dict[str, str]
) -> None:
    """The vendor hears every frame and answers each with words; the socket drops them, and the
    final, read from the session, is the one a socket with partials gets."""
    with (
        realtime_app(tmp_path, vendor.url) as client,
        client.websocket_connect("/ws/stt?partials=0", headers=auth) as socket,
    ):
        socket.send_bytes(SILENCE)
        socket.send_bytes(SILENCE)
        socket.send_json({"type": "stt.stop"})
        frames = collect_until(socket, "stt.final")
    assert frames == [
        {"type": "stt.final", "text": f"final {2 * len(SILENCE)} bytes.", "language": "auto"}
    ]
    kinds = [event["type"] for event in vendor.requests]
    assert kinds.count("input_audio_buffer.append") == 2
    assert kinds[-2:] == ["input_audio_buffer.commit", "session.finish"]


def test_realtime_without_partials_still_reports_the_vendor_refusing(
    tmp_path: Path, failing_vendor: FakeRealtimeServer, auth: dict[str, str]
) -> None:
    with (
        realtime_app(tmp_path, failing_vendor.url) as client,
        client.websocket_connect("/ws/stt?partials=0", headers=auth) as socket,
    ):
        socket.send_bytes(SILENCE)
        error = drain_until(socket, "stt.error")
    assert "quota exhausted" in error["message"]
