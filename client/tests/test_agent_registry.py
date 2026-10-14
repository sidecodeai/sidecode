"""The agent registry: which ids exist, what dispatches, and what does not."""

from __future__ import annotations

from pathlib import Path

import pytest

from rc_client.agents import registry
from rc_client.agents.registry import AGENT_IDS, DetectContext, RunnerSpec, detect_all, runner_for
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry


def test_every_id_names_a_package_with_the_three_plugin_symbols() -> None:
    assert AGENT_IDS == ("claude", "codex", "grok", "pi")
    for agent in AGENT_IDS:
        plugin = registry.plugin(agent)
        assert agent == plugin.AGENT
        assert callable(plugin.detect)
        assert callable(plugin.build_runner)


def test_an_unknown_agent_is_unsupported_rather_than_an_import_error() -> None:
    with pytest.raises(RcError) as caught:
        registry.plugin("gemini")
    assert caught.value.code == "unsupported"
    assert "gemini" in caught.value.message


async def test_building_a_runner_for_an_unknown_agent_is_unsupported(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "gemini")
    with pytest.raises(RcError) as caught:
        await runner_for("gemini", spec)
    assert caught.value.code == "unsupported"


async def test_detection_answers_for_every_id_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    agents = await detect_all(DetectContext(codex_daemon_ready=False))
    assert [info.agent for info in agents] == list(AGENT_IDS)


async def test_a_grok_runner_needs_the_binary(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "grok")
    with pytest.raises(RcError) as caught:
        await runner_for("grok", spec)
    assert caught.value.code == "agent_unavailable"


def _spec(tmp_path: Path, agent: str) -> RunnerSpec:
    registry_db = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent=agent, cwd=str(tmp_path))

    async def publish(frame: dict[str, object]) -> None:
        return None

    channel = SessionChannel(registry_db, session, publish)
    entry = SessionEntry(session=session, channel=channel)

    async def on_turn_end() -> None:
        return None

    async def on_session_id(session_id: str) -> None:
        return None

    return RunnerSpec(
        entry=entry,
        info=AgentInfo(agent=agent, available=False),
        resume=None,
        on_turn_end=on_turn_end,
        on_session_id=on_session_id,
    )
