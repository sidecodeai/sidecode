"""Codex approval and question requests, driven without a live `codex app-server`.

The Codex sandbox only escalates when it refuses an action, which depends on the developer's own
`~/.codex/config.toml`, so the round trip cannot be forced from an integration test. These tests
drive `CodexRunner._on_request` directly, which is the exact entry point the JSON-RPC reader calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex.adapter import CodexRunner
from rc_client.agents.codex.models import parse_catalog
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.helpers import event_validator


class Recorder:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def states(self) -> list[str]:
        return [event["state"] for event in self.events("status")]


def build(tmp_path: Path) -> tuple[CodexRunner, Recorder, Registry]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="codex", cwd=str(tmp_path))
    registry.upsert_session(session)
    recorder = Recorder()
    channel = SessionChannel(registry, session, recorder)
    runner = CodexRunner(
        channel,
        binary="/bin/codex",
        cwd=str(tmp_path),
        catalog=parse_catalog([]),
    )
    return runner, recorder, channel.registry


async def resolve(runner: CodexRunner, option_id: str) -> None:
    """Answer the approval as soon as the handler has published it."""
    for _ in range(200):
        if runner._pending:
            request_id = next(iter(runner._pending))
            assert await runner.approve(request_id, option_id, None)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no approval was published")


@pytest.mark.parametrize(
    ("method", "tool", "tool_kind"),
    [
        ("item/commandExecution/requestApproval", "Shell", "shell"),
        ("item/fileChange/requestApproval", "Apply patch", "edit"),
        ("item/permissions/requestApproval", "Permissions", "other"),
    ],
)
async def test_an_approval_request_becomes_a_conformant_approval_block(
    tmp_path: Path, method: str, tool: str, tool_kind: str
) -> None:
    runner, recorder, registry = build(tmp_path)
    params = {"command": ["rm", "-rf", "/etc"], "cwd": str(tmp_path), "reason": "outside sandbox"}

    handled = asyncio.create_task(runner._on_request(method, params))
    await resolve(runner, "accept")
    result = await handled

    pending, resolved = recorder.events("approval")
    assert pending["status"] == "pending"
    assert pending["tool"] == tool
    assert pending["tool_kind"] == tool_kind
    assert pending["title"]
    assert pending["block_id"] == resolved["block_id"], "the resolved card replaces the pending one"
    assert pending["request_id"] == resolved["request_id"]
    styles = {option["style"] for option in pending["options"]}
    assert "primary" in styles and "danger" in styles
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == {"option_id": "accept", "by": "remote"}
    assert recorder.states() == ["needs_approval", "running"]

    validator = event_validator()
    if validator is not None:
        for event in (pending, resolved):
            validator.validate(event)

    if method.endswith("permissions/requestApproval"):
        assert result["scope"] == "turn"
    else:
        assert result == {"decision": "accept"}
    registry.close()


async def test_declining_is_reported_back_to_codex(tmp_path: Path) -> None:
    runner, recorder, registry = build(tmp_path)
    handled = asyncio.create_task(
        runner._on_request("item/commandExecution/requestApproval", {"command": ["rm", "-rf", "/"]})
    )
    await resolve(runner, "decline")
    assert await handled == {"decision": "decline"}
    assert recorder.events("approval")[-1]["decision"]["option_id"] == "decline"
    registry.close()


async def test_a_session_grant_widens_the_permission_scope(tmp_path: Path) -> None:
    runner, _, registry = build(tmp_path)
    handled = asyncio.create_task(
        runner._on_request("item/permissions/requestApproval", {"permissions": {"network": True}})
    )
    await resolve(runner, "acceptForSession")
    result = await handled
    assert result == {"permissions": {"network": True}, "scope": "session"}
    registry.close()


async def test_an_unknown_request_method_is_refused(tmp_path: Path) -> None:
    runner, _, registry = build(tmp_path)
    with pytest.raises(Exception) as caught:
        await runner._on_request("item/nonsense", {})
    assert getattr(caught.value, "code", "") == "unsupported"
    registry.close()
