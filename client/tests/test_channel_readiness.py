"""The bridge holds what the daemon injects until Claude Code listens for it.

The bridge registers at `initialize`, so the device can inject at once, but
Claude Code starts listening for channel messages only after it has listed the
server's tools, and drops anything that came before without a word.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rc_client.channel import bridge as bridge_module
from rc_client.channel import wire
from rc_client.channel.bridge import ChannelBridge
from tests.test_attach_channel import RecordingLink

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"clientInfo": {"name": "claude-code", "version": "2.1.303"}},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every frame the bridge would have written to Claude Code."""
    frames: list[dict[str, Any]] = []
    monkeypatch.setattr(bridge_module, "write", frames.append)
    return frames


def delivered(frames: list[dict[str, Any]]) -> list[str]:
    return [
        str(frame["params"]["meta"]["message_id"])
        for frame in frames
        if frame.get("method") == "notifications/claude/channel"
    ]


async def test_messages_injected_as_the_terminal_attaches_wait_and_keep_their_order(
    written: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = ChannelBridge(RecordingLink(), "sess-1", "/repo")  # type: ignore[arg-type]
    bridge.on_rpc(INITIALIZE)
    await bridge.on_daemon(wire.inject("m1", "first"))
    await bridge.on_daemon(wire.inject("m2", "second"))
    assert delivered(written) == [], "Claude Code is not listening yet"

    monkeypatch.setattr(bridge_module, "LISTEN_GRACE", 0.01)
    listening = asyncio.create_task(bridge.listen())
    assert bridge.on_rpc(TOOLS_LIST) == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    await asyncio.wait_for(listening, timeout=2)
    assert delivered(written) == ["m1", "m2"]

    await bridge.on_daemon(wire.inject("m3", "third"))
    assert delivered(written) == ["m1", "m2", "m3"], "once it listens, at once"


async def test_a_cli_that_never_lists_tools_still_gets_its_messages(
    written: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "LISTEN_TIMEOUT", 0.05)
    monkeypatch.setattr(bridge_module, "LISTEN_GRACE", 0.01)
    bridge = ChannelBridge(RecordingLink(), "sess-1", "/repo")  # type: ignore[arg-type]
    bridge.on_rpc(INITIALIZE)
    await bridge.on_daemon(wire.inject("m1", "first"))
    await asyncio.wait_for(bridge.listen(), timeout=2)
    assert delivered(written) == ["m1"]


async def test_a_verdict_is_never_held(written: list[dict[str, Any]]) -> None:
    """Claude Code asks for one only once it listens, so it is answered at once."""
    bridge = ChannelBridge(RecordingLink(), "sess-1", "/repo")  # type: ignore[arg-type]
    bridge.on_rpc(INITIALIZE)
    await bridge.on_daemon(wire.permission("r1", "allow"))
    assert [frame["params"] for frame in written] == [{"request_id": "r1", "behavior": "allow"}]
