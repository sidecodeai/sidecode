"""Amendment A59: a `codex exec` run is not a session.

Scripts and automations run `codex exec`, which records `originator:
"codex_exec"` and `source: "exec"`. The device publishes none of them, withdraws
the ones it published before, by the same route A18 takes, and never reads their
rollouts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.mirror import MirrorService
from tests.test_codex_daemon_sessions import Harness, harness, thread_row

__all__ = ["harness"]


def write_rollout(root: Path, thread_id: str, originator: str, source: str) -> Path:
    directory = root / "2026" / "11" / "16"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-11-16T08-00-00-{thread_id}.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": thread_id,
            "cwd": "/repo",
            "originator": originator,
            "source": source,
        },
    }
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return path


def published(hub: SessionHub, session_id: str, origin: str = "terminal") -> SessionEntry:
    """A Codex session stored by a device from before A59."""
    return hub.register_mirrored(
        Session(
            session_id=session_id,
            device_id="dev-1",
            agent="codex",
            cwd="/repo",
            title="Nightly digest",
            state="idle",
            origin=origin,  # type: ignore[arg-type]
            control="none",
        )
    )


def test_an_exec_rollout_is_never_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_rollout(tmp_path, "t-exec", "codex_exec", "exec")
    write_rollout(tmp_path, "t-tui", "codex-tui", "cli")
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    assert [info.thread_id for info in codex_rollouts.discover()] == ["t-tui"]


def test_a_rollout_says_whose_its_thread_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_rollout(tmp_path, "t-exec", "codex_exec", "exec")
    write_rollout(tmp_path, "t-tui", "codex-tui", "cli")
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    assert codex_rollouts.owned_on_disk({"t-exec", "t-tui", "t-gone"}) == {
        "t-exec": False,
        "t-tui": True,
    }


async def test_the_daemon_index_never_adopts_an_exec_run(harness: Harness) -> None:
    harness.daemon.replies["thread/list"] = {
        "data": [
            thread_row("t-exec", originator="codex_exec", source="exec"),
            thread_row("t-tui", originator="codex-tui", source="cli"),
        ]
    }
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True
    assert "t-exec" not in harness.hub.entries
    assert "t-tui" in harness.hub.entries


async def test_exec_runs_published_before_are_withdrawn_from_their_rollouts(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollout on disk answers, so the daemon is not asked about any of them."""
    root = tmp_path / "codex-sessions"
    write_rollout(root, "t-exec", "codex_exec", "exec")
    write_rollout(root, "t-tui", "codex-tui", "cli")
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", root)
    for session_id in ("t-exec", "t-tui"):
        entry = published(harness.hub, session_id)
        await entry.channel.emit("user_message", block_id="b1", text="x", source="terminal")
    harness.daemon.replies["thread/list"] = {"data": []}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True

    assert sorted(harness.hub.entries) == ["t-tui"]
    removed = [f["session_id"] for f in harness.frames if f.get("type") == "session.removed"]
    assert removed == ["t-exec"]
    assert [method for method, _ in harness.daemon.calls if method == "thread/read"] == []


async def test_without_the_daemon_the_mirror_withdraws_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "codex-sessions"
    write_rollout(root, "t-exec", "codex_exec", "exec")
    write_rollout(root, "t-tui", "codex-tui", "cli")
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", root)
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    registry = Registry(tmp_path / "state.sqlite3")
    hub = SessionHub(registry, publish, "dev-1", lambda: [])
    for session_id in ("t-exec", "t-tui"):
        published(hub, session_id)
    # One the device launched is its own, whatever any file says.
    published(hub, "t-remote", origin="remote")
    mirror = MirrorService(hub)
    try:
        await mirror._prune_foreign_codex()
        assert sorted(hub.entries) == ["t-remote", "t-tui"]
        assert [f["session_id"] for f in frames if f.get("type") == "session.removed"] == ["t-exec"]
    finally:
        await hub.close()
        registry.close()
