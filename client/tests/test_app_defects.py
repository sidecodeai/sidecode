"""Regression tests for the defects the web and iOS integration reported."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import transcripts
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.agents.codex import rollouts
from rc_client.config import (
    DEFAULT_CLAUDE_SETTING_SOURCES,
    DEFAULT_MIRROR_MAX_AGE_DAYS,
    DEFAULT_MIRROR_MAX_SESSIONS,
    Config,
    load_config,
    save_config,
)
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.mirror import MirrorService
from tests.test_hub import FakeRunner, add_session, build_hub


async def _publish(frame: dict[str, Any]) -> None:
    return None


# ------------------------------------------------------------------- defect 1


def test_the_claude_runner_never_loads_user_level_settings(tmp_path: Path) -> None:
    """The machine's own settings must not decide a remote user's approvals.

    `permissions.defaultMode: "auto"` and `PermissionRequest` hooks live in
    `~/.claude/settings.json`; loading them resolves the permission in-process
    and cancels the `can_use_tool` call the remote decision arrives on.
    """
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)
    runner = ClaudeRunner(
        SessionChannel(registry, session, _publish),
        binary=None,
        cwd=str(tmp_path),
        permission_mode="default",
    )
    sources = runner._options().setting_sources
    assert sources is not None
    assert "user" not in sources
    assert sources == list(DEFAULT_CLAUDE_SETTING_SOURCES)
    registry.close()


def test_setting_sources_can_be_widened_by_configuration(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)
    runner = ClaudeRunner(
        SessionChannel(registry, session, _publish),
        binary=None,
        cwd=str(tmp_path),
        setting_sources=["user", "project", "local"],
    )
    assert runner._options().setting_sources == ["user", "project", "local"]
    registry.close()


def test_the_explicit_permission_mode_is_always_passed_through(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)
    for mode in ("default", "acceptEdits", "plan", "bypassPermissions"):
        runner = ClaudeRunner(
            SessionChannel(registry, session, _publish),
            binary=None,
            cwd=str(tmp_path),
            permission_mode=mode,
        )
        assert runner._options().permission_mode == mode
    registry.close()


# ------------------------------------------------------------------- defect 2


def _write_transcript(root: Path, session_id: str, cwd: Path) -> Path:
    project = root / "-".join(cwd.parts[1:])[:60]
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    row = {
        "type": "user",
        "uuid": "u1",
        "sessionId": session_id,
        "cwd": str(cwd),
        "message": {"role": "user", "content": "hello from the terminal"},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


async def test_a_session_we_created_is_not_re_imported_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`origin` is persisted, so this must hold when the runner is long gone."""
    projects = tmp_path / "projects"
    workdir = tmp_path / "repo"
    workdir.mkdir()
    _write_transcript(projects, "remote-1", workdir)
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", projects)

    hub, _, registry = build_hub(tmp_path)
    persisted = Session(
        session_id="remote-1",
        device_id="dev-1",
        agent="claude",
        cwd=str(workdir),
        origin="remote",
        control="none",
    )
    registry.upsert_session(persisted)
    hub.load()
    assert hub.entries["remote-1"].runner is None, "a restart leaves no runner"

    mirror = MirrorService(hub)
    mirror._adopt_claude(transcripts.discover())
    assert "remote-1" not in mirror._claude
    assert hub.entries["remote-1"].session.origin == "remote"
    registry.close()


async def test_a_terminal_session_is_still_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    workdir = tmp_path / "repo"
    workdir.mkdir()
    _write_transcript(projects, "terminal-1", workdir)
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", projects)

    hub, _, registry = build_hub(tmp_path)
    mirror = MirrorService(hub)
    mirror._adopt_claude(transcripts.discover())
    assert "terminal-1" in mirror._claude
    assert hub.entries["terminal-1"].session.origin == "terminal"
    registry.close()


async def test_a_live_remote_session_is_never_adopted(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    entry = add_session(hub)
    assert isinstance(entry.runner, FakeRunner)
    mirror = MirrorService(hub)
    assert mirror._adoptable(entry) is False
    entry.runner = None
    assert mirror._adoptable(entry) is False, "origin remote stays ours without a runner"
    entry.session.origin = "terminal"
    assert mirror._adoptable(entry) is True
    registry.close()


# ------------------------------------------------------------------- defect 3


async def test_an_interrupted_turn_keeps_the_sessions_usage(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd="/tmp")
    registry.upsert_session(session)
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    channel = SessionChannel(registry, session, publish)
    await channel.begin_turn("remote")
    await channel.end_turn(
        "completed", 100, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    )
    assert session.usage is not None
    assert session.usage["total_tokens"] == 15

    await channel.begin_turn("remote")
    await channel.end_turn(
        "interrupted", 20, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    assert session.usage["total_tokens"] == 15, "zero usage must not erase the totals"

    completed = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "turn_completed"
    ]
    assert completed[-1]["stop_reason"] == "interrupted"
    assert completed[-1]["usage"]["total_tokens"] == 15
    await channel.close()
    registry.close()


async def test_a_real_measurement_still_replaces_the_totals(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd="/tmp")
    registry.upsert_session(session)
    channel = SessionChannel(registry, session, _publish)
    await channel.begin_turn("remote")
    await channel.end_turn(
        "completed", 10, {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    )
    await channel.begin_turn("remote")
    await channel.end_turn(
        "interrupted", 10, {"input_tokens": 40, "output_tokens": 2, "total_tokens": 42}
    )
    assert session.usage is not None
    assert session.usage["total_tokens"] == 42
    await channel.close()
    registry.close()


async def test_a_turn_with_no_usage_at_all_leaves_the_chip_alone(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd="/tmp")
    registry.upsert_session(session)
    channel = SessionChannel(registry, session, _publish)
    await channel.begin_turn("remote")
    await channel.end_turn(
        "completed", 10, {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4}
    )
    await channel.begin_turn("remote")
    await channel.end_turn("interrupted", 5, None)
    assert session.usage is not None
    assert session.usage["total_tokens"] == 4
    await channel.close()
    registry.close()


# ------------------------------------------------------------------- defect 4


def test_the_initial_import_is_capped_by_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    workdir = tmp_path / "repo"
    workdir.mkdir()
    for index in range(60):
        _write_transcript(projects, f"s{index:03d}", workdir)
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", projects)

    assert len(transcripts.discover()) == DEFAULT_MIRROR_MAX_SESSIONS
    assert len(transcripts.discover(limit=5)) == 5


def test_the_initial_import_is_capped_by_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    workdir = tmp_path / "repo"
    workdir.mkdir()
    recent = _write_transcript(projects, "recent", workdir)
    old = _write_transcript(projects, "old", workdir)
    ancient = time.time() - 40 * 86400
    import os

    os.utime(old, (ancient, ancient))
    monkeypatch.setattr(transcripts, "PROJECTS_DIR", projects)

    found = {info.session_id for info in transcripts.discover()}
    assert found == {"recent"}
    widened = {info.session_id for info in transcripts.discover(max_age_days=60)}
    assert widened == {"recent", "old"}
    assert recent.exists()


def test_rollout_discovery_takes_the_same_bounds() -> None:
    assert rollouts.MAX_ROLLOUTS == DEFAULT_MIRROR_MAX_SESSIONS
    assert rollouts.MAX_AGE_DAYS == DEFAULT_MIRROR_MAX_AGE_DAYS
    assert transcripts.MAX_TRANSCRIPTS == DEFAULT_MIRROR_MAX_SESSIONS


def test_the_mirror_passes_its_configured_limits_through(tmp_path: Path) -> None:
    from rc_client.config import MirrorConfig

    hub, _, registry = build_hub(tmp_path)
    mirror = MirrorService(hub, MirrorConfig(max_sessions=7, max_age_days=3))
    assert mirror.limits.max_sessions == 7
    assert mirror.limits.max_age_days == 3
    assert MirrorService(hub).limits.max_sessions == DEFAULT_MIRROR_MAX_SESSIONS
    registry.close()


# ------------------------------------------------------------------- config


def test_the_new_sections_round_trip_through_config_toml() -> None:
    from rc_client.config import ClaudeConfig, MirrorConfig

    save_config(
        Config(
            gateway_origin="https://rc.example.com",
            device_id="dev-1",
            device_token="tok",
            name="mac",
            mirror=MirrorConfig(max_sessions=7, max_age_days=3),
            claude=ClaudeConfig(setting_sources=["user", "project"]),
        )
    )
    loaded = load_config()
    assert loaded.mirror.max_sessions == 7
    assert loaded.mirror.max_age_days == 3
    assert loaded.claude.setting_sources == ["user", "project"]


def test_a_config_without_the_new_sections_gets_the_defaults() -> None:
    from rc_client.config import config_path, ensure_dirs

    ensure_dirs()
    config_path().write_text(
        'gateway_origin = "https://rc.example.com"\n'
        'device_id = "dev-1"\ndevice_token = "tok"\nname = "mac"\n',
        encoding="utf-8",
    )
    loaded = load_config()
    assert loaded.mirror.max_sessions == DEFAULT_MIRROR_MAX_SESSIONS
    assert loaded.mirror.max_age_days == DEFAULT_MIRROR_MAX_AGE_DAYS
    assert loaded.claude.setting_sources == list(DEFAULT_CLAUDE_SETTING_SOURCES)


def test_nonsense_limits_fall_back_to_the_defaults() -> None:
    from rc_client.config import config_path, ensure_dirs

    ensure_dirs()
    config_path().write_text(
        'gateway_origin = "https://rc.example.com"\n'
        'device_id = "dev-1"\ndevice_token = "tok"\nname = "mac"\n'
        "[mirror]\nmax_sessions = 0\nmax_age_days = -4\n"
        '[claude]\nsetting_sources = ["user", "bogus"]\n',
        encoding="utf-8",
    )
    loaded = load_config()
    assert loaded.mirror.max_sessions == DEFAULT_MIRROR_MAX_SESSIONS
    assert loaded.mirror.max_age_days == DEFAULT_MIRROR_MAX_AGE_DAYS
    assert loaded.claude.setting_sources == ["user"]


# ----------------------------- A12 and the send path's own round trips


REQUEST = "5c0a7f36-2d19-4b8c-a4e3-71f0c9d2e845"


class StubSDK:
    """Enough of the SDK client for one `send`."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def disconnect(self) -> None:
        return None


def claude_runner(tmp_path: Path, frames: list[dict[str, Any]], **kwargs: Any) -> ClaudeRunner:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    channel = SessionChannel(registry, session, publish)
    return ClaudeRunner(channel, binary=None, cwd=str(tmp_path), **kwargs)


def kinds(frames: list[dict[str, Any]]) -> list[str]:
    return [
        frame["event"]["kind"] if frame.get("type") == "session.event" else str(frame.get("type"))
        for frame in frames
    ]


async def test_a_claude_send_carries_the_request_id_as_its_block(tmp_path: Path) -> None:
    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames)
    runner._client = StubSDK()  # type: ignore[assignment]
    await runner.send("go", block_id=REQUEST)
    bubbles = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]
    assert [bubble["block_id"] for bubble in bubbles] == [REQUEST]
    runner.channel.registry.close()


async def test_a_claude_send_shows_the_message_before_restarting_for_effort(
    tmp_path: Path,
) -> None:
    """A new effort level restarts the CLI, which must not hold up the bubble."""
    frames: list[dict[str, Any]] = []
    runner = claude_runner(tmp_path, frames, effort="high")
    runner._client = StubSDK()  # type: ignore[assignment]
    runner._applied_effort = "low"

    async def restart() -> None:
        frames.append({"type": "restarted"})

    runner._reconnect = restart  # type: ignore[method-assign]
    await runner.send("go", block_id=REQUEST)
    order = kinds(frames)
    assert order.index("user_message") < order.index("restarted")
    assert order.index("restarted") < order.index("turn_started")
    runner.channel.registry.close()


async def test_a_codex_send_carries_the_request_id_as_its_block(tmp_path: Path) -> None:
    from rc_client.agents.codex.adapter import CodexRunner
    from rc_client.agents.codex.models import parse_catalog

    frames: list[dict[str, Any]] = []
    registry = Registry(tmp_path / "codex.sqlite3")
    session = Session(session_id="s2", device_id="d", agent="codex", cwd=str(tmp_path))
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    class StubServer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append(method)
            return {"turn": {"id": "turn-1"}}

    runner = CodexRunner(
        SessionChannel(registry, session, publish),
        binary="/bin/codex",
        cwd=str(tmp_path),
        catalog=parse_catalog([]),
    )
    runner._server = StubServer()  # type: ignore[assignment]
    runner._thread_id = "t1"
    await runner.send("go", block_id=REQUEST)
    bubbles = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]
    assert [bubble["block_id"] for bubble in bubbles] == [REQUEST]
    order = kinds(frames)
    assert order.index("user_message") < order.index("turn_started")
    registry.close()


STEERED = "0d6b8e29-4a17-4c3f-9b52-8e1a6f70d3c4"


def build_codex_runner(tmp_path: Path, frames: list[dict[str, Any]]) -> Any:
    """A `CodexRunner` whose app-server answers every request the same way."""
    from rc_client.agents.codex.adapter import CodexRunner
    from rc_client.agents.codex.models import parse_catalog

    registry = Registry(tmp_path / "codex.sqlite3")
    session = Session(session_id="s3", device_id="d", agent="codex", cwd=str(tmp_path))
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    class StubServer:
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"turn": {"id": "turn-1"}}

    runner = CodexRunner(
        SessionChannel(registry, session, publish),
        binary="/bin/codex",
        cwd=str(tmp_path),
        catalog=parse_catalog([]),
    )
    runner._server = StubServer()  # type: ignore[assignment]
    runner._thread_id = "t1"
    runner._turn_id = "turn-1"
    return runner


def user_messages(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "user_message"
    ]


async def echo(runner: Any, text: str, item_id: str) -> None:
    """Replay a prompt the way Codex does: the same item, started and completed."""
    item = {"id": item_id, "type": "userMessage", "content": [{"type": "text", "text": text}]}
    for method in ("item/started", "item/completed"):
        await runner._on_notification(method, {"item": dict(item)})


async def test_a_codex_steer_waits_for_codex_to_read_the_prompt(tmp_path: Path) -> None:
    """A14: a steered bubble belongs where the agent took the message."""
    frames: list[dict[str, Any]] = []
    runner = build_codex_runner(tmp_path, frames)
    assert await runner.steer("also this", block_id=STEERED) is True
    assert user_messages(frames) == []

    await runner._on_notification(
        "item/completed", {"item": {"id": "a1", "type": "agentMessage", "text": "still going"}}
    )
    assert user_messages(frames) == []

    await echo(runner, "also this", "u1")
    bubbles = user_messages(frames)
    assert [bubble["block_id"] for bubble in bubbles] == [STEERED]
    said = [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "assistant_text"
    ]
    # Which is the whole point: it sorts after what Codex was already saying.
    assert bubbles[0]["first_seq"] > said[-1]["first_seq"]
    runner.channel.registry.close()


async def test_a_codex_steer_the_turn_never_read_lands_at_its_end(tmp_path: Path) -> None:
    frames: list[dict[str, Any]] = []
    runner = build_codex_runner(tmp_path, frames)
    assert await runner.steer("also this", block_id=STEERED) is True
    runner._interrupting = True
    await runner._on_notification(
        "turn/completed", {"turn": {"id": "turn-1", "status": "interrupted"}}
    )
    assert [bubble["block_id"] for bubble in user_messages(frames)] == [STEERED]
    order = kinds(frames)
    assert order.index("user_message") < order.index("notice") < order.index("turn_completed")
    runner.channel.registry.close()
