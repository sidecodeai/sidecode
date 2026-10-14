"""Amendment A11: the WebSocket-over-Unix-socket client for the shared daemon."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex.daemon.rpc import DaemonClient, handshake_ok, initialize_params
from rc_client.errors import RcError
from tests.fake_codex_daemon import FakeDaemon


class Sink:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[tuple[Any, str, dict[str, Any]]] = []
        self.reconnects = 0
        self.reply: dict[str, Any] = {"decision": "accept"}
        self.fail = False

    async def notification(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))

    async def request(self, request_id: Any, method: str, params: dict[str, Any]) -> Any:
        self.requests.append((request_id, method, params))
        if self.fail:
            raise RcError("unsupported", "no")
        return self.reply

    async def connected(self) -> None:
        self.reconnects += 1


@pytest.fixture
async def daemon(socket_dir: Path) -> Any:
    server = FakeDaemon(socket_dir / "codex.sock")
    await server.start()
    yield server
    await server.stop()


async def _client(daemon: FakeDaemon, sink: Sink) -> DaemonClient:
    client = DaemonClient(
        "0.1.0",
        on_notification=sink.notification,
        on_request=sink.request,
        on_connected=sink.connected,
        socket=daemon.path,
    )
    await client.start()
    return client


async def _settle(predicate: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def test_initialize_names_this_client_and_asks_for_the_experimental_surface() -> None:
    params = initialize_params("9.9.9")
    assert params["clientInfo"]["name"] == "remote-control"
    assert params["clientInfo"]["version"] == "9.9.9"
    assert params["capabilities"] == {"experimentalApi": True}


async def test_the_handshake_completes_and_is_followed_by_initialized(daemon: FakeDaemon) -> None:
    sink = Sink()
    client = await _client(daemon, sink)
    try:
        assert client.connected
        await daemon.wait_for_call("initialized")
        assert daemon.sent("initialize")[0]["clientInfo"]["name"] == "remote-control"
    finally:
        await client.close()


async def test_requests_correlate_by_id_even_when_answers_interleave(daemon: FakeDaemon) -> None:
    sink = Sink()
    daemon.replies["thread/list"] = {"data": [{"id": "t1"}]}
    daemon.replies["thread/loaded/list"] = {"data": ["t1"]}
    client = await _client(daemon, sink)
    try:
        first, second = await asyncio.gather(
            client.request("thread/list", {}), client.request("thread/loaded/list", {})
        )
        assert first == {"data": [{"id": "t1"}]}
        assert second == {"data": ["t1"]}
    finally:
        await client.close()


async def test_a_large_frame_survives_the_default_websocket_ceiling(daemon: FakeDaemon) -> None:
    sink = Sink()
    payload = "x" * (2 * 1024 * 1024)
    daemon.replies["thread/items/list"] = {"data": [{"id": "i1", "text": payload}]}
    client = await _client(daemon, sink)
    try:
        result = await client.request("thread/items/list", {})
        assert result["data"][0]["text"] == payload
    finally:
        await client.close()


async def test_notifications_reach_the_sink(daemon: FakeDaemon) -> None:
    sink = Sink()
    client = await _client(daemon, sink)
    try:
        await daemon.notify("thread/status/changed", {"threadId": "t1"})
        await _settle(lambda: sink.notifications)
        assert sink.notifications[0][0] == "thread/status/changed"
    finally:
        await client.close()


async def test_a_server_request_is_answered_with_the_handler_result(daemon: FakeDaemon) -> None:
    sink = Sink()
    client = await _client(daemon, sink)
    try:
        await daemon.ask(7, "item/commandExecution/requestApproval", {"threadId": "t1"})
        answer = await asyncio.wait_for(daemon.answers.get(), timeout=2)
        assert answer == {"jsonrpc": "2.0", "id": 7, "result": {"decision": "accept"}}
    finally:
        await client.close()


async def test_a_failing_handler_answers_with_an_error_rather_than_nothing(
    daemon: FakeDaemon,
) -> None:
    sink = Sink()
    sink.fail = True
    client = await _client(daemon, sink)
    try:
        await daemon.ask(9, "item/commandExecution/requestApproval", {"threadId": "t1"})
        answer = await asyncio.wait_for(daemon.answers.get(), timeout=2)
        assert answer["id"] == 9
        assert answer["error"]["code"] == -32000
    finally:
        await client.close()


async def test_a_slow_request_handler_never_blocks_the_stream(daemon: FakeDaemon) -> None:
    """An approval waits on a person; notifications must keep arriving meanwhile."""
    sink = Sink()
    release = asyncio.Event()

    async def slow(request_id: Any, method: str, params: dict[str, Any]) -> Any:
        await release.wait()
        return {"decision": "accept"}

    client = DaemonClient(
        "0.1.0", on_notification=sink.notification, on_request=slow, socket=daemon.path
    )
    await client.start()
    try:
        await daemon.ask(1, "item/commandExecution/requestApproval", {"threadId": "t1"})
        await daemon.notify("turn/started", {"threadId": "t1"})
        await _settle(lambda: sink.notifications)
        assert sink.notifications[0][0] == "turn/started"
        release.set()
        answer = await asyncio.wait_for(daemon.answers.get(), timeout=2)
        assert answer["result"] == {"decision": "accept"}
    finally:
        await client.close()


async def test_a_dropped_connection_reconnects_and_reports_it(daemon: FakeDaemon) -> None:
    sink = Sink()
    client = await _client(daemon, sink)
    try:
        await daemon.drop()
        await _settle(lambda: sink.reconnects >= 1, timeout=8)
        assert daemon.connections >= 2
        assert client.connected
    finally:
        await client.close()


async def test_pending_requests_fail_rather_than_hang_when_the_socket_drops(
    daemon: FakeDaemon,
) -> None:
    sink = Sink()

    def never(method: str, params: dict[str, Any]) -> Any:
        if method == "thread/read":
            raise RuntimeError("drop instead of answering")
        return None

    daemon.responder = never
    client = await _client(daemon, sink)
    try:
        with pytest.raises(RcError) as caught:
            await client.request("thread/read", {"threadId": "t1"}, timeout=5)
        assert caught.value.code == "agent_unavailable"
    finally:
        await client.close()


async def test_handshake_ok_is_true_for_a_live_socket_and_false_for_a_missing_one(
    daemon: FakeDaemon, socket_dir: Path
) -> None:
    assert await handshake_ok("0.1.0", daemon.path) is True
    assert await handshake_ok("0.1.0", socket_dir / "absent.sock") is False
