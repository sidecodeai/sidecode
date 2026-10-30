"""The gateway's socket to the Live API: what it sends, how it fails, what it never logs."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from rc_gateway.live.config import LiveConfig
from rc_gateway.live.errors import LiveError
from rc_gateway.live.prompts import session_config
from rc_gateway.live.upstream import connect_upstream

from .fakes import API_KEY, FakeLiveServer, until


class SilentServer:
    """A service that takes the connection and the ``session.start`` and never answers."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.open = 0
        self.received: list[dict[str, Any]] = []
        self._reply = reply

    async def handler(self, connection: ServerConnection) -> None:
        self.open += 1
        try:
            async for raw in connection:
                self.received.append(json.loads(raw))
                if self._reply is not None:
                    await connection.send(json.dumps(self._reply))
        finally:
            self.open -= 1


def config_for(port: int) -> LiveConfig:
    return LiveConfig(api_key=API_KEY, base_url=f"http://127.0.0.1:{port}")


async def test_a_connect_cancelled_while_waiting_for_session_started_closes_the_socket() -> None:
    silent = SilentServer()
    async with serve(silent.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = config_for(port)
        task = asyncio.create_task(connect_upstream(config, session_config(config)))
        await until(lambda: bool(silent.received))
        assert silent.open == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await until(lambda: silent.open == 0)


async def test_a_service_that_answers_with_an_error_is_refused_and_the_socket_closed() -> None:
    refusing = SilentServer(reply={"type": "error", "error": {"code": "invalid_model"}})
    async with serve(refusing.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = config_for(port)
        with pytest.raises(LiveError) as refused:
            await connect_upstream(config, session_config(config))
        assert refused.value.code == "upstream_refused"
        await until(lambda: refusing.open == 0)


async def test_only_something_that_looks_like_an_error_code_is_logged_from_a_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hostile = {"type": "error", "error": {"code": "ZZ-FREE TEXT-ZZ, not a code"}}
    refusing = SilentServer(reply=hostile)
    async with serve(refusing.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = config_for(port)
        with caplog.at_level(logging.INFO), pytest.raises(LiveError):
            await connect_upstream(config, session_config(config))
    [record] = [r for r in caplog.records if r.getMessage() == "live session did not start"]
    assert record.fields["error_code"] == "unknown"  # type: ignore[attr-defined]
    assert "ZZ-FREE" not in repr([r.__dict__ for r in caplog.records])


REGION_REFUSAL = {
    "error": {
        "message": "ZZ-MESSAGE-ZZ Country, region, or territory not supported",
        "type": "request_forbidden",
        "param": None,
        "code": "unsupported_country_region_territory",
    }
}


@pytest.mark.parametrize(
    ("body", "logged"),
    [
        (json.dumps(REGION_REFUSAL), "unsupported_country_region_territory"),
        (json.dumps({"error": {"code": "ZZ-FREE TEXT-ZZ, not a code"}}), "unknown"),
        (json.dumps({"error": "ZZ-MESSAGE-ZZ"}), "unknown"),
        ("<html>ZZ-MESSAGE-ZZ</html>", "unknown"),
        ("", "unknown"),
    ],
)
async def test_a_refused_connection_logs_the_error_code_and_nothing_else_of_the_answer(
    caplog: pytest.LogCaptureFixture, body: str, logged: str
) -> None:
    raw = body.encode()

    def refuse(connection: ServerConnection, request: Request) -> Response:
        headers = Headers(
            {
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "X-Request-Id": "ZZ-HEADER-ZZ",
            }
        )
        return Response(403, "Forbidden", headers, raw)

    async with serve(SilentServer().handler, "127.0.0.1", 0, process_request=refuse) as server:
        config = config_for(server.sockets[0].getsockname()[1])
        with caplog.at_level(logging.INFO), pytest.raises(LiveError) as refused:
            await connect_upstream(config, session_config(config))
    assert refused.value.code == "upstream_refused"
    ours = [r for r in caplog.records if r.name.startswith("rc_gateway")]
    [record] = [r for r in ours if r.getMessage() == "live upstream refused the connection"]
    assert record.fields == {"status": 403, "error_code": logged}  # type: ignore[attr-defined]
    text = repr([(r.getMessage(), getattr(r, "fields", {})) for r in ours])
    for secret in ("ZZ-MESSAGE-ZZ", "ZZ-HEADER-ZZ", "ZZ-FREE"):
        assert secret not in text


async def test_a_service_that_closes_instead_of_starting_is_refused() -> None:
    refusing = SilentServer(reply={"type": "session.closed", "reason": "content"})
    async with serve(refusing.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = config_for(port)
        with pytest.raises(LiveError) as refused:
            await connect_upstream(config, session_config(config))
        assert refused.value.code == "upstream_refused"


async def test_a_service_that_never_starts_is_given_up_on_and_the_socket_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rc_gateway.live.upstream.STARTED_TIMEOUT_SECONDS", 0.2)
    silent = SilentServer()
    async with serve(silent.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        config = config_for(port)
        with pytest.raises(LiveError) as late:
            await connect_upstream(config, session_config(config))
        assert late.value.code == "upstream_unavailable"
        await until(lambda: silent.open == 0)


async def test_what_crosses_the_socket_is_never_in_the_logs_even_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = FakeLiveServer()
    server.start()
    try:
        caplog.set_level(logging.DEBUG)
        config = LiveConfig(api_key=API_KEY, base_url=server.base_url)
        upstream, _ = await connect_upstream(config, session_config(config))
        server.push({"type": "session.output_transcript.delta", "delta": "ZZ-TRANSCRIPT-ZZ"})
        server.push(
            {
                "type": "response.event",
                "event": {"type": "response.output_text.delta", "delta": "ZZ-TEXT-ZZ"},
            }
        )
        for _ in range(2):
            await upstream.receive()
        await upstream.send(
            {
                "type": "response.item.create",
                "item": {"type": "function_call_output", "call_id": "c", "output": "ZZ-TOOL-ZZ"},
            }
        )
        await asyncio.sleep(0.1)
        await upstream.close()
    finally:
        server.stop()
    # The fake service runs in this process and logs what it receives under its own names; what is
    # asserted is about the gateway's side of the connection.
    ours = [r for r in caplog.records if not r.name.startswith("websockets.server")]
    assert not [r for r in ours if r.name.startswith("websockets")]
    text = repr([(r.name, r.getMessage(), getattr(r, "fields", {})) for r in ours])
    for secret in (API_KEY, "ZZ-TRANSCRIPT-ZZ", "ZZ-TEXT-ZZ", "ZZ-TOOL-ZZ"):
        assert secret not in text


async def test_the_session_start_is_the_first_thing_sent_and_carries_the_session() -> None:
    server = FakeLiveServer()
    server.start()
    try:
        config = LiveConfig(api_key=API_KEY, base_url=server.base_url, voice="marin")
        upstream, started = await connect_upstream(config, session_config(config))
        assert started["session"]["id"] == "live_sess_test"
        first = server.received[0]
        assert first["type"] == "session.start"
        assert first["session"]["model"] == "gpt-live-1"
        assert first["session"]["audio"] == {
            "format": {"type": "audio/pcm", "rate": 24000},
            "output": {"voice": "marin"},
        }
        assert server.headers["authorization"] == f"Bearer {API_KEY}"
        assert server.path == "/v1/live/sessions"
        await upstream.close()
    finally:
        server.stop()
