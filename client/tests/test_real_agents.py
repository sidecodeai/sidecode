"""Opt-in smoke tests that drive the real agents installed on this machine.

Skipped unless `RC_REAL_AGENTS=1`. Each test sends one tiny prompt so a full
run costs a single short turn per agent.

    RC_REAL_AGENTS=1 uv run pytest -q tests/test_real_agents.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.agents.claude.runtime import resolve_binary as resolve_claude
from rc_client.agents.codex.adapter import CodexRunner
from rc_client.agents.codex.models import catalog_cache
from rc_client.agents.codex.runtime import resolve_binary as resolve_codex
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel

PROMPT = "Reply with exactly OK"
TURN_TIMEOUT = 180.0

pytestmark = pytest.mark.skipif(
    os.environ.get("RC_REAL_AGENTS") != "1",
    reason="set RC_REAL_AGENTS=1 to drive the installed agents",
)


class Collector:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.turn_completed = asyncio.Event()

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "turn_completed":
            self.turn_completed.set()

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]


def build(tmp_path: Path, agent: str) -> tuple[SessionChannel, Collector, Registry, Session]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(
        session_id=f"pending-{agent}", device_id="dev-1", agent=agent, cwd=str(tmp_path)
    )
    registry.upsert_session(session)
    collector = Collector()
    channel = SessionChannel(registry, session, collector)
    channel.start()
    return channel, collector, registry, session


def assert_ok_turn(collector: Collector) -> None:
    started = collector.events("turn_started")
    completed = collector.events("turn_completed")
    assert started, "no turn_started event"
    assert completed, "no turn_completed event"
    assert completed[0]["stop_reason"] == "completed"
    usage = completed[0].get("usage") or {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert key in usage, f"usage is missing {key}"

    finals = [event for event in collector.events("assistant_text") if event.get("done")]
    assert finals, "no completed assistant_text block"
    assert "OK" in "".join(event.get("text", "") for event in finals)


async def test_claude_answers_a_one_word_prompt(tmp_path: Path) -> None:
    binary = resolve_claude()
    if binary is None:
        pytest.skip("claude is not installed")
    channel, collector, registry, session = build(tmp_path, "claude")
    captured: list[str] = []

    async def on_session_id(real_id: str) -> None:
        captured.append(real_id)

    runner = ClaudeRunner(channel, binary=binary, cwd=str(tmp_path), on_session_id=on_session_id)
    await runner.start()
    try:
        await runner.send(PROMPT)
        await asyncio.wait_for(collector.turn_completed.wait(), timeout=TURN_TIMEOUT)
    finally:
        await runner.close()
        await channel.close()

    assert_ok_turn(collector)
    assert captured, "the SDK never reported a session id"
    assert captured[0] != session.session_id
    transcript = Path.home() / ".claude" / "projects"
    assert any(transcript.glob(f"*/{captured[0]}.jsonl")), "no transcript for the reported id"
    registry.close()


async def test_codex_answers_a_one_word_prompt(tmp_path: Path) -> None:
    binary = resolve_codex()
    if binary is None:
        pytest.skip("codex is not installed")
    channel, collector, registry, session = build(tmp_path, "codex")
    captured: list[str] = []

    async def on_session_id(real_id: str) -> None:
        captured.append(real_id)

    catalog = await catalog_cache.get(binary)
    runner = CodexRunner(
        channel,
        binary=binary,
        cwd=str(tmp_path),
        catalog=catalog,
        permission_mode="never",
        on_session_id=on_session_id,
    )
    await runner.start()
    try:
        await runner.send(PROMPT)
        await asyncio.wait_for(collector.turn_completed.wait(), timeout=TURN_TIMEOUT)
    finally:
        await runner.close()
        await channel.close()

    assert_ok_turn(collector)
    assert captured, "codex never reported a thread id"
    assert captured[0] != session.session_id
    registry.close()


async def test_claude_waits_for_a_remote_approval_before_writing(tmp_path: Path) -> None:
    """A `permission_mode: "default"` session must never be approved locally.

    This machine's own `~/.claude/settings.json` carries auto-approval, so this
    is the regression test for it: the card has to stay pending until the remote
    user answers, and the file must not exist before that.
    """
    binary = resolve_claude()
    if binary is None:
        pytest.skip("claude is not installed")
    channel, collector, registry, _ = build(tmp_path, "claude")
    target = tmp_path / "hello.txt"

    runner = ClaudeRunner(
        channel,
        binary=binary,
        cwd=str(tmp_path),
        permission_mode="default",
    )
    await runner.start()
    try:
        await runner.send(
            "Use the Write tool right now to create hello.txt containing hi. "
            "Do not explain, just call Write."
        )

        async def pending_approval() -> dict[str, Any]:
            while True:
                for event in collector.events("approval"):
                    if event["status"] == "pending":
                        return event
                await asyncio.sleep(0.05)

        approval = await asyncio.wait_for(pending_approval(), timeout=TURN_TIMEOUT)
        assert approval["tool"] == "Write"
        assert {option["id"] for option in approval["options"]} == {
            "allow",
            "allow_session",
            "deny",
        }

        # The card must still be pending, and nothing written, a beat later:
        # the reported failure expired it about ten milliseconds in.
        await asyncio.sleep(2.0)
        statuses = [event["status"] for event in collector.events("approval")]
        assert statuses == ["pending"], f"approval did not stay pending: {statuses}"
        assert not target.exists(), "the file was written before the remote user approved"

        assert await runner.approve(approval["request_id"], "allow", None)
        await asyncio.wait_for(collector.turn_completed.wait(), timeout=TURN_TIMEOUT)
    finally:
        await runner.close()
        await channel.close()

    resolved = [event for event in collector.events("approval") if event["status"] == "resolved"]
    assert resolved, "the approval was never resolved"
    assert resolved[-1]["decision"] == {"option_id": "allow", "by": "remote"}
    assert target.exists(), "the file was not written after approval"
    registry.close()
