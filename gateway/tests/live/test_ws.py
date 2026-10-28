"""``WS /ws/live`` against a real local WebSocket standing in for the Live API."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from rc_gateway.app import create_app
from rc_gateway.auth import SESSION_COOKIE_NAME
from rc_gateway.live.service import LiveService
from rc_gateway.state import GatewayState

from ..conftest import (
    ORIGIN,
    PASSWORD,
    add_member,
    close_code_for,
    device_hello,
    drain_until,
    enroll_device,
    session_summary,
    sign_in,
)
from .fakes import API_KEY, FRAME_BYTES, LOUD, QUIET, FakeLiveServer, pcm_ms

pytestmark = pytest.mark.timeout(30)

SESSION = "dddddddd-0000-4000-8000-000000000001"


class Wire:
    """The browser's side of a socket: what has arrived, read one message at a time."""

    def __init__(self, ws: WebSocketTestSession) -> None:
        self.ws = ws
        self.messages: list[dict[str, Any]] = []
        self.audio = b""
        self.closed: tuple[int, str] | None = None

    def pump(self) -> None:
        message = self.ws.receive()
        if message["type"] == "websocket.close":
            self.closed = (int(message.get("code", 1000)), str(message.get("reason", "")))
        elif message.get("bytes") is not None:
            self.audio += message["bytes"]
        else:
            self.messages.append(json.loads(message["text"]))

    def until(self, kind: str) -> dict[str, Any]:
        """Read until a message of this ``type`` has arrived, and return it."""
        for _ in range(2000):
            for message in self.messages:
                if message.get("type") == kind:
                    self.messages.remove(message)
                    return message
            assert self.closed is None, f"closed {self.closed} before {kind!r}: {self.messages}"
            self.pump()
        raise AssertionError(f"{kind!r} never arrived")

    def until_closed(self) -> tuple[int, str]:
        for _ in range(2000):
            if self.closed is not None:
                return self.closed
            self.pump()
        raise AssertionError("the socket never closed")

    def until_audio(self, size: int) -> None:
        for _ in range(2000):
            if len(self.audio) >= size:
                return
            assert self.closed is None
            self.pump()
        raise AssertionError("the audio never arrived")

    def send_audio(self, frame: bytes, count: int = 1) -> None:
        for _ in range(count):
            self.ws.send_bytes(frame)


@pytest.fixture
def server() -> Iterator[FakeLiveServer]:
    fake = FakeLiveServer()
    fake.start()
    yield fake
    fake.stop()


def settings(monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, **overrides: str) -> None:
    values = {
        "LIVE_ASSISTANT_ENABLED": "true",
        "LIVE_OPENAI_API_KEY": API_KEY,
        "LIVE_OPENAI_BASE_URL": server.base_url,
        "LIVE_ALLOWED_USERS": "admin,dana",
        "LIVE_COOLDOWN_SECONDS": "0",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def live(
    monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, state: GatewayState
) -> Iterator[TestClient]:
    settings(monkeypatch, server)
    with TestClient(create_app(state)) as client:
        yield client


@pytest.fixture
def admin(live: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {sign_in(live, 'admin', PASSWORD)}"}


def b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def usage_records(server: FakeLiveServer, caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The usage lines logged. A call writes its line last, after both sockets are closed, so the
    service seeing its connection close does not mean the line is there yet."""

    def logged() -> list[dict[str, Any]]:
        return [
            r.fields  # type: ignore[attr-defined]
            for r in caplog.records
            if r.getMessage() == "live call usage"
        ]

    server.wait_for(lambda: bool(logged()))
    return logged()


# ---- who may connect ----


def test_a_socket_without_a_login_is_closed_4401(live: TestClient, server: FakeLiveServer) -> None:
    assert close_code_for(live, "/ws/live") == 4401
    assert server.connections == 0


def test_a_cookie_socket_from_a_foreign_origin_is_closed_4403(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    token = admin["Authorization"].split()[1]
    live.cookies.set(SESSION_COOKIE_NAME, token)
    assert close_code_for(live, "/ws/live", headers={"Origin": "https://evil.example"}) == 4403
    assert server.connections == 0
    with live.websocket_connect("/ws/live", headers={"Origin": ORIGIN}) as ws:
        wire = Wire(ws)
        assert wire.until("ready")["sample_rate"] == 24000
        ws.send_json({"type": "hangup"})
        wire.until("ended")


def test_an_account_that_is_not_listed_is_refused_before_the_service_is_dialled(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    erin = add_member(live, admin, "erin")
    with live.websocket_connect("/ws/live", headers=erin) as ws:
        wire = Wire(ws)
        assert wire.until("ended") == {"type": "ended", "reason": "not_allowed"}
        assert wire.until_closed()[0] == 4403
    assert server.connections == 0


def test_the_upgrade_rate_is_limited_per_address(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    codes = []
    for _ in range(12):
        with live.websocket_connect("/ws/live", headers={"Authorization": "Bearer nope"}) as ws:
            wire = Wire(ws)
            codes.append(wire.until_closed()[0])
    assert 4429 in codes
    assert server.connections == 0


# ---- a call ----


def test_a_call_is_relayed_between_the_browser_and_the_service(
    live: TestClient,
    admin: dict[str, str],
    server: FakeLiveServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="rc_gateway")
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        ready = wire.until("ready")
        assert ready["sample_rate"] == 24000
        assert ready["frame_ms"] == 20

        assert server.headers["authorization"] == f"Bearer {API_KEY}"
        assert server.path == "/v1/live/sessions"
        [start] = server.of("session.start")
        session = start["session"]
        assert session["model"] == "gpt-live-1"
        assert session["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert session["delegation"]["type"] == "responses"
        names = {tool["name"] for tool in session["delegation"]["responses"]["tools"]}
        assert names == {
            "list_sessions",
            "get_session_status",
            "list_pending_decisions",
            "get_session_context",
            "prepare_message",
            "confirm_send",
        }
        assert "<|SpawnThinking|>" in session["instructions"]

        # The microphone goes up as base64 appends, in order.
        wire.send_audio(QUIET, 2)
        wire.send_audio(LOUD, 1)
        server.wait_for(lambda: len(server.of("session.input_audio.append")) == 3)
        appended = [base64.b64decode(e["audio"]) for e in server.of("session.input_audio.append")]
        assert appended == [QUIET, QUIET, LOUD]

        # The assistant's speech comes down as binary frames, paced.
        server.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
        wire.until_audio(200 * 48)
        assert wire.audio == pcm_ms(200)

        ws.send_json({"type": "hangup"})
        assert wire.until("ended") == {"type": "ended", "reason": "hangup"}
        assert wire.until_closed()[0] == 1000

    server.wait_for(lambda: server.open_connections == 0)
    assert server.types().count("session.close") == 1
    # The key never left the gateway.
    ours = [r for r in caplog.records if r.name.startswith("rc_gateway")]
    assert ours
    everything = repr([(r.getMessage(), getattr(r, "fields", {})) for r in ours])
    assert API_KEY not in everything
    assert API_KEY not in json.dumps(wire.messages)


def test_the_usage_of_a_call_is_logged_once_with_what_openai_reported(
    live: TestClient,
    admin: dict[str, str],
    server: FakeLiveServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        server.push(
            {
                "type": "response.event",
                "event": {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "output": [],
                        "usage": {"input_tokens": 900, "output_tokens": 40},
                    },
                },
            }
        )
        server.push({"type": "session.usage.updated", "usage": {"seconds": 3}})
        ws.send_json({"type": "hangup"})
        wire.until("ended")
    server.wait_for(lambda: server.open_connections == 0)
    [record] = usage_records(server, caplog)
    assert record["account"] == "admin"
    assert record["reason"] == "hangup"
    assert record["voice_seconds"] == 12.0
    assert record["usage_final"] is True
    assert (record["backend_input_tokens"], record["backend_output_tokens"]) == (900, 40)
    assert record["session_id"] == "live_sess_test"
    assert len(record["call_id"]) == 32


def test_the_working_hint_the_session_on_screen_and_the_players_counters_travel_the_socket(
    live: TestClient,
    admin: dict[str, str],
    server: FakeLiveServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")

    def backend(kind: str) -> dict[str, Any]:
        return {"type": "response.event", "event": {"type": kind, "response": {"id": "resp_1"}}}

    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        server.push(backend("response.created"))
        assert wire.until("working") == {"type": "working", "on": True}
        server.push(backend("response.completed"))
        assert wire.until("working") == {"type": "working", "on": False}

        # Neither command is answered, and neither ends the call, bad values included.
        ws.send_json({"type": "focus", "device_id": "dev", "session_id": "sess"})
        ws.send_json({"type": "focus", "device_id": 7, "session_id": None})
        ws.send_json(
            {"type": "playback", "underruns": 2, "gap_ms": 340, "restarts": 1, "delay_ms": 600}
        )
        ws.send_json({"type": "playback", "underruns": -1, "gap_ms": "x", "delay_ms": "soon"})
        ws.send_json({"type": "hangup"})
        assert wire.until("ended")["reason"] == "hangup"
    server.wait_for(lambda: server.open_connections == 0)
    [record] = usage_records(server, caplog)
    assert (record["browser_underruns"], record["browser_gap_ms"]) == (2, 340)
    assert (record["browser_restarts"], record["browser_delay_ms"]) == (1, 600)
    assert (record["speech_ms"], record["input_transcripts"]) == (0, 0)
    assert record["backend_ms_max"] is not None
    assert record["replies"] == 0


def test_the_person_talking_over_the_assistant_flushes_the_browser(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        server.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(4000))})
        wire.until_audio(1920)
        wire.send_audio(LOUD, 10)
        wire.until("flush")
        assert wire.until("state")["speaker"] in {"assistant", "user"}
        ws.send_json({"type": "hangup"})
        wire.until("ended")


def test_captions_and_drafts_travel_over_the_socket_and_the_button_sends(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    enrolled = enroll_device(live, admin)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    summary = session_summary(SESSION, enrolled["device_id"], title="Fix flaky auth test")
    with live.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(sessions=[summary]))
        assert device.receive_json()["type"] == "hello_ack"
        with live.websocket_connect("/ws/live", headers=admin) as ws:
            wire = Wire(ws)
            wire.until("ready")
            ws.send_json({"type": "captions", "enabled": True})
            server.push({"type": "session.input_transcript.delta", "delta": "What is running?"})
            assert wire.until("caption") == {
                "type": "caption",
                "speaker": "user",
                "delta": "What is running?",
            }

            server.push(_call("list_sessions", {}, "c1"))
            server.wait_for(lambda: "response.create" in server.types())
            output = json.loads(server.of("response.item.create")[0]["item"]["output"])
            assert [s["title"] for s in output["sessions"]] == ["Fix flaky auth test"]

            server.push(_call("prepare_message", {"session": "auth", "text": "Go on"}, "c2"))
            draft = wire.until("draft")["draft"]
            assert draft["text"] == "Go on"

            ws.send_json({"type": "draft.send", "draft_id": draft["id"]})
            request = drain_until(device, "session.send")
            assert request["session_id"] == SESSION
            assert request["text"] == "Go on"
            device.send_json(
                {
                    "type": "reply",
                    "id": request["id"],
                    "from": request["from"],
                    "ok": True,
                    "result": {"accepted": "sent"},
                }
            )
            result = wire.until("draft.result")
            assert result["ok"] is True
            assert wire.until("draft")["draft"] is None
            ws.send_json({"type": "hangup"})
            wire.until("ended")


def test_the_time_left_is_sent_while_the_call_runs(
    monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, state: GatewayState
) -> None:
    monkeypatch.setattr("rc_gateway.live.call.TIME_TICK_SECONDS", 0.05)
    monkeypatch.setattr("rc_gateway.live.call.WATCH_INTERVAL_SECONDS", 0.02)
    settings(monkeypatch, server)
    with TestClient(create_app(state)) as client:
        headers = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        with client.websocket_connect("/ws/live", headers=headers) as ws:
            wire = Wire(ws)
            wire.until("ready")
            assert wire.until("time")["seconds_left"] > 0
            ws.send_json({"type": "hangup"})
            wire.until("ended")


# ---- policy ----


def test_a_second_call_on_one_account_is_refused_and_the_first_goes_on(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as first:
        one = Wire(first)
        one.until("ready")
        with live.websocket_connect("/ws/live", headers=admin) as second:
            two = Wire(second)
            assert two.until("ended") == {"type": "ended", "reason": "call_active"}
            assert two.until_closed()[0] == 4409
        assert server.connections == 1
        one.send_audio(QUIET)
        server.wait_for(lambda: len(server.of("session.input_audio.append")) == 1)
        first.send_json({"type": "hangup"})
        one.until("ended")


def test_the_gateway_caps_calls_across_accounts(
    monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, state: GatewayState
) -> None:
    settings(monkeypatch, server, LIVE_MAX_CONCURRENT_CALLS="1")
    with TestClient(create_app(state)) as client:
        admin = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        dana = add_member(client, admin, "dana")
        with client.websocket_connect("/ws/live", headers=admin) as first:
            Wire(first).until("ready")
            with client.websocket_connect("/ws/live", headers=dana) as second:
                two = Wire(second)
                assert two.until("ended")["reason"] == "too_busy"
                assert two.until_closed()[0] == 4429
            first.send_json({"type": "hangup"})


def test_an_account_waits_out_a_cooldown_after_a_call(
    monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, state: GatewayState
) -> None:
    settings(monkeypatch, server, LIVE_COOLDOWN_SECONDS="60")
    with TestClient(create_app(state)) as client:
        admin = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        with client.websocket_connect("/ws/live", headers=admin) as ws:
            wire = Wire(ws)
            wire.until("ready")
            ws.send_json({"type": "hangup"})
            wire.until("ended")
        with client.websocket_connect("/ws/live", headers=admin) as again:
            retry = Wire(again)
            assert retry.until("ended")["reason"] == "cooldown"
            assert retry.until_closed()[0] == 4429


def test_the_daily_allowance_from_the_settings_bounds_the_call(
    monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, state: GatewayState
) -> None:
    settings(monkeypatch, server, LIVE_DAILY_SECONDS="30")
    with TestClient(create_app(state)) as client:
        admin = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        with client.websocket_connect("/ws/live", headers=admin) as ws:
            wire = Wire(ws)
            assert 25 <= wire.until("ready")["seconds_left"] <= 30
            ws.send_json({"type": "hangup"})
            wire.until("ended")


def test_a_slot_is_free_again_after_a_call(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    for _ in range(3):
        with live.websocket_connect("/ws/live", headers=admin) as ws:
            wire = Wire(ws)
            wire.until("ready")
            ws.send_json({"type": "hangup"})
            wire.until("ended")
    assert server.connections == 3


# ---- every way out closes both ends ----


def test_the_browser_vanishing_closes_the_service_at_once(
    live: TestClient,
    admin: dict[str, str],
    server: FakeLiveServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        Wire(ws).until("ready")
    server.wait_for(lambda: server.open_connections == 0)
    assert "session.close" in server.types()
    assert [record["reason"] for record in usage_records(server, caplog)] == ["browser_closed"]


def test_the_service_dropping_the_socket_closes_the_browser_with_a_reason(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        server.drop()
        assert wire.until("ended") == {"type": "ended", "reason": "upstream_lost"}
        assert wire.until_closed()[0] == 4502
    server.wait_for(lambda: server.open_connections == 0)


def test_the_service_ending_the_session_is_passed_on(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        server.push({"type": "session.closed", "reason": "expired", "usage": {"seconds": 1200}})
        assert wire.until("ended")["reason"] == "expired"
        assert wire.until_closed()[0] == 1000


def test_the_gateway_stopping_closes_both_ends(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        service: LiveService = cast(FastAPI, live.app).state.live
        assert live.portal is not None
        live.portal.call(service.close)
        assert wire.until("ended") == {"type": "ended", "reason": "gateway_stopping"}
        assert wire.until_closed()[0] == 1001
    server.wait_for(lambda: server.open_connections == 0)
    assert "session.close" in server.types()


def test_signing_out_ends_the_call(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        assert live.post("/api/logout", headers=admin).status_code in (200, 204)
        assert wire.until("ended")["reason"] == "session_revoked"
    server.wait_for(lambda: server.open_connections == 0)


@pytest.mark.parametrize(
    "send",
    [
        lambda ws: ws.send_bytes(b"\x00" * 3),
        lambda ws: ws.send_bytes(b"\x00" * 100),
        lambda ws: ws.send_bytes(b"\x00" * 20000),
        lambda ws: ws.send_text("not json"),
        lambda ws: ws.send_text("[1, 2]"),
        lambda ws: ws.send_text(json.dumps({"type": "captions", "pad": "x" * 5000})),
    ],
    ids=["odd", "tiny", "huge", "not-json", "not-object", "oversize"],
)
def test_a_malformed_frame_closes_the_call_with_a_policy_violation(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer, send: Any
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        send(ws)
        assert wire.until("ended")["reason"] == "protocol_error"
        assert wire.until_closed()[0] == 1008
    server.wait_for(lambda: server.open_connections == 0)
    assert "session.close" in server.types()


def test_a_flood_of_audio_closes_the_call(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        wire.send_audio(QUIET, 400)
        assert wire.until("ended")["reason"] == "protocol_error"
        assert wire.until_closed()[0] == 1008


# ---- the service reporting errors ----


def test_an_error_from_the_service_reaches_the_browser_as_a_code_and_never_as_its_text(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        server.push(
            {
                "type": "error",
                "error": {"code": "rate_limit_exceeded", "message": "ZZ-PRIVATE-TEXT-ZZ"},
            }
        )
        assert wire.until("notice") == {"type": "notice", "code": "rate_limit_exceeded"}
        assert "ZZ-PRIVATE-TEXT-ZZ" not in json.dumps(wire.messages)
        ws.send_json({"type": "hangup"})
        wire.until("ended")


def test_errors_in_a_row_from_the_service_end_the_call(
    live: TestClient, admin: dict[str, str], server: FakeLiveServer
) -> None:
    with live.websocket_connect("/ws/live", headers=admin) as ws:
        wire = Wire(ws)
        wire.until("ready")
        for _ in range(3):
            server.push({"type": "error", "error": {"code": "server_error"}})
        assert wire.until("ended")["reason"] == "upstream_errors"
        assert wire.until_closed()[0] == 4502
    server.wait_for(lambda: server.open_connections == 0)
    assert "session.close" in server.types()


# ---- the service refusing ----


def test_a_service_that_refuses_the_key_is_a_bad_gateway_and_holds_no_slot(
    monkeypatch: pytest.MonkeyPatch, state: GatewayState
) -> None:
    refusing = FakeLiveServer(reject_status=401)
    refusing.start()
    try:
        settings(monkeypatch, refusing)
        with TestClient(create_app(state)) as client:
            admin = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
            for _ in range(3):
                with client.websocket_connect("/ws/live", headers=admin) as ws:
                    wire = Wire(ws)
                    assert wire.until("ended") == {"type": "ended", "reason": "upstream_refused"}
                    assert wire.until_closed()[0] == 4502
            assert refusing.of("session.start") == []
    finally:
        refusing.stop()


def test_a_service_that_is_not_there_is_a_bad_gateway(
    monkeypatch: pytest.MonkeyPatch, server: FakeLiveServer, state: GatewayState
) -> None:
    settings(monkeypatch, server, LIVE_OPENAI_BASE_URL="http://127.0.0.1:9")
    with TestClient(create_app(state)) as client:
        admin = {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}
        with client.websocket_connect("/ws/live", headers=admin) as ws:
            wire = Wire(ws)
            assert wire.until("ended")["reason"] == "upstream_unavailable"
            assert wire.until_closed()[0] == 4502


def _call(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "type": "response.event",
        "event": {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            },
        },
    }


assert FRAME_BYTES == 960
