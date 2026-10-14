"""Amendment A21: the speed tier a session runs at, beside model and effort."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude.plugin import detect as detect_claude
from rc_client.agents.codex.daemon.rpc import DaemonClient
from rc_client.agents.codex.daemon.session import CodexDaemonSession
from rc_client.agents.codex.models import ModelCatalog, parse_catalog
from rc_client.agents.codex.plugin import detect as detect_codex
from rc_client.agents.registry import DetectContext
from rc_client.errors import RcError
from rc_client.models import UNSET, AgentInfo, Choice, Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests.fake_codex_daemon import FakeDaemon
from tests.helpers import event_validator, object_validator
from tests.test_discovery import fake_binary
from tests.test_hub import FakeRunner

THREAD = "01a096bc-78ae-7f82-87e5-97cdd81bd442"

CATALOGUE: list[dict[str, Any]] = [
    {
        "id": "gpt-5.4-codex",
        "displayName": "GPT-5.4 Codex",
        "isDefault": True,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
        "serviceTiers": [
            {"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}
        ],
    },
    {
        "id": "gpt-5.4-spark",
        "displayName": "GPT-5.4 Spark",
        "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
    },
]


# ------------------------------------------------------------------ catalogue


def test_the_catalogue_reads_the_service_tiers_a_model_offers() -> None:
    catalog = parse_catalog(CATALOGUE)
    assert [choice.id for choice in catalog.speeds] == ["priority"]
    assert [choice.label for choice in catalog.speeds] == ["Fast"]
    assert [choice.id for choice in catalog.speeds_for("gpt-5.4-codex")] == ["priority"]
    assert catalog.speeds_for("gpt-5.4-spark") == []


def test_a_catalogue_without_tiers_advertises_none() -> None:
    catalog = parse_catalog([item for item in CATALOGUE if item["id"] == "gpt-5.4-spark"])
    assert catalog.speeds == []
    assert catalog.speeds_for("gpt-5.4-spark") == []
    # A model the catalogue never described gets the union, which is empty here.
    assert catalog.speeds_for("gpt-6") == []


def test_the_union_keeps_catalogue_order_and_never_repeats_a_tier() -> None:
    catalog = parse_catalog(
        [
            {"id": "a", "serviceTiers": [{"id": "priority", "name": "Fast"}]},
            {
                "id": "b",
                "serviceTiers": [
                    {"id": "flex", "name": "Flex"},
                    {"id": "priority", "name": "Fast"},
                ],
            },
        ]
    )
    assert [choice.id for choice in catalog.speeds] == ["priority", "flex"]
    assert [choice.id for choice in catalog.speeds_for("b")] == ["flex", "priority"]


def test_a_standard_tier_in_the_catalogue_is_not_a_choice() -> None:
    """`default` is how Codex spells the standard speed, which needs no entry."""
    catalog = parse_catalog([{"id": "a", "serviceTiers": [{"id": "default", "name": "Standard"}]}])
    assert catalog.speeds == []


# ------------------------------------------------------------------ discovery


async def test_codex_advertises_the_catalogue_tiers_and_claude_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_binary(tmp_path / "bin", "codex", "0.154.0")
    monkeypatch.setenv("RC_CODEX_BIN", str(binary))

    async def catalogue(_: str) -> ModelCatalog:
        return parse_catalog(CATALOGUE)

    monkeypatch.setattr("rc_client.agents.codex.plugin.catalog_cache.get", catalogue)
    codex = await detect_codex(DetectContext(codex_daemon_ready=False))
    assert [choice.id for choice in codex.speeds] == ["priority"]

    monkeypatch.setenv("RC_CLAUDE_BIN", str(fake_binary(tmp_path / "bin", "claude", "2.1.269")))
    claude = await detect_claude(DetectContext())
    assert claude.speeds == []


async def test_a_codex_without_a_catalogue_advertises_no_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_binary(tmp_path / "bin", "codex", "0.154.0")
    monkeypatch.setenv("RC_CODEX_BIN", str(binary))

    async def empty(_: str) -> ModelCatalog:
        return ModelCatalog()

    monkeypatch.setattr("rc_client.agents.codex.plugin.catalog_cache.get", empty)
    info = await detect_codex(DetectContext(codex_daemon_ready=False))
    assert info.speeds == []


# ------------------------------------------------------------------- the hub


def agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice("default", "Default")],
            default_model="default",
            permission_modes=[Choice("default", "Ask before edits")],
            default_permission_mode="default",
            capabilities=["interrupt", "queue"],
        ),
        AgentInfo(
            agent="codex",
            available=True,
            path="/bin/codex",
            models=[Choice("gpt-5.4-codex", "GPT-5.4 Codex"), Choice("gpt-5.4-spark", "Spark")],
            default_model="gpt-5.4-codex",
            permission_modes=[Choice("on-request", "Ask when needed")],
            default_permission_mode="on-request",
            efforts=[Choice("low", "Low"), Choice("medium", "Medium")],
            speeds=[Choice("priority", "Fast")],
            capabilities=["interrupt", "queue", "steer"],
        ),
    ]


def build_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SessionHub, Registry]:
    async def catalogue(_: str) -> ModelCatalog:
        return parse_catalog(CATALOGUE)

    monkeypatch.setattr("rc_client.sessions.hub.catalog_cache.get", catalogue)
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    return SessionHub(registry, publish, "dev-1", agents), registry


def add_session(hub: SessionHub, agent: str, model: str | None) -> SessionEntry:
    session = Session(
        session_id="sess-1",
        device_id="dev-1",
        agent=agent,
        cwd="/repo",
        state="idle",
        model=model,
    )
    hub.registry.upsert_session(session)
    channel = SessionChannel(hub.registry, session, hub.publish)
    entry = SessionEntry(session=session, channel=channel)
    entry.runner = FakeRunner(channel)
    hub.entries[session.session_id] = entry
    return entry


async def test_the_hub_takes_a_tier_the_model_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    entry = add_session(hub, "codex", "gpt-5.4-codex")
    result = await hub.set_options({"session_id": "sess-1", "speed": "priority"})
    assert result["session"]["speed"] == "priority"
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert runner.settings == [(None, None, None, "priority")]
    registry.close()


async def test_a_null_tier_takes_the_session_back_to_the_standard_speed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    entry = add_session(hub, "codex", "gpt-5.4-codex")
    entry.session.speed = "priority"
    result = await hub.set_options({"session_id": "sess-1", "speed": None})
    assert result["session"]["speed"] is None
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert runner.settings == [(None, None, None, None)]
    registry.close()


async def test_a_request_that_never_mentions_the_tier_leaves_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    entry = add_session(hub, "codex", "gpt-5.4-codex")
    entry.session.speed = "priority"
    result = await hub.set_options({"session_id": "sess-1", "effort": "low"})
    assert result["session"]["speed"] == "priority"
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    assert runner.settings == [(None, None, "low", UNSET)]
    registry.close()


async def test_an_unknown_tier_is_a_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    add_session(hub, "codex", "gpt-5.4-codex")
    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "speed": "turbo"})
    assert caught.value.code == "bad_request"
    registry.close()


async def test_a_model_with_no_faster_tier_refuses_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    add_session(hub, "codex", "gpt-5.4-spark")
    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "speed": "priority"})
    assert caught.value.code == "unsupported"
    assert caught.value.message == "this model has no faster tier"
    registry.close()


async def test_the_model_the_same_request_sets_is_the_one_a_tier_is_checked_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    add_session(hub, "codex", "gpt-5.4-codex")
    with pytest.raises(RcError) as caught:
        await hub.set_options(
            {"session_id": "sess-1", "model": "gpt-5.4-spark", "speed": "priority"}
        )
    assert caught.value.code == "unsupported"
    registry.close()


async def test_claude_refuses_a_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    add_session(hub, "claude", "default")
    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "speed": "priority"})
    assert caught.value.code == "unsupported"
    assert caught.value.message == "claude has no speed tiers"
    registry.close()


async def test_a_new_session_is_refused_a_tier_before_anything_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, registry = build_hub(tmp_path, monkeypatch)
    with pytest.raises(RcError) as caught:
        await hub.create({"agent": "codex", "cwd": str(tmp_path), "speed": "turbo"})
    assert caught.value.code == "bad_request"
    with pytest.raises(RcError) as refused:
        await hub.create(
            {
                "agent": "codex",
                "cwd": str(tmp_path),
                "model": "gpt-5.4-spark",
                "speed": "priority",
            }
        )
    assert refused.value.code == "unsupported"
    assert hub.entries == {}
    registry.close()


# --------------------------------------------------------- the shared daemon


class Session1:
    """A `CodexDaemonSession` wired to a fake daemon, with its events kept."""

    def __init__(self, registry: Registry, daemon: FakeDaemon, client: DaemonClient) -> None:
        self.registry = registry
        self.daemon = daemon
        self.client = client
        self.frames: list[dict[str, Any]] = []

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.daemon.calls if name == method]


async def daemon_session(
    tmp_path: Path,
    socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    speed: str | None,
    started_tier: Any = None,
) -> tuple[CodexDaemonSession, Session1]:
    daemon = FakeDaemon(socket_dir / "codex.sock")
    await daemon.start()
    monkeypatch.setenv("RC_CODEX_DAEMON_SOCKET", str(daemon.path))
    daemon.replies["thread/start"] = {
        "thread": {"id": THREAD},
        "model": "gpt-5.4-codex",
        "reasoningEffort": "medium",
        "approvalPolicy": "on-request",
        "serviceTier": started_tier,
    }

    async def notification(method: str, params: dict[str, Any]) -> None:
        return None

    async def request(request_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    client = DaemonClient("0.1.0", on_notification=notification, on_request=request)
    await client.start()
    registry = Registry(tmp_path / "state.sqlite3")
    harness = Session1(registry, daemon, client)

    async def publish(frame: dict[str, Any]) -> None:
        harness.frames.append(frame)

    session = Session(session_id="sess-1", device_id="dev-1", agent="codex", cwd="/repo")
    channel = SessionChannel(registry, session, publish)
    runner = CodexDaemonSession(
        channel,
        client,
        cwd="/repo",
        catalog=parse_catalog(CATALOGUE),
        model="gpt-5.4-codex",
        speed=speed,
    )
    return runner, harness


async def close(harness: Session1) -> None:
    await harness.client.close()
    await harness.daemon.stop()
    harness.registry.close()


async def test_a_new_thread_is_asked_for_its_tier_after_it_starts(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`thread/start` accepts `serviceTier` and ignores it, so the tier follows."""
    runner, harness = await daemon_session(tmp_path, socket_dir, monkeypatch, speed="priority")
    await runner.start()
    assert harness.calls("thread/settings/update") == [
        {"threadId": THREAD, "serviceTier": "priority"}
    ]
    await close(harness)


async def test_a_thread_that_already_runs_at_the_tier_is_left_alone(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, harness = await daemon_session(
        tmp_path, socket_dir, monkeypatch, speed="priority", started_tier="priority"
    )
    await runner.start()
    assert harness.calls("thread/settings/update") == []
    await close(harness)


async def test_a_standard_session_asks_for_nothing(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, harness = await daemon_session(tmp_path, socket_dir, monkeypatch, speed=None)
    await runner.start()
    assert harness.calls("thread/settings/update") == []
    await close(harness)


async def test_setting_and_clearing_the_tier_reaches_the_daemon(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, harness = await daemon_session(tmp_path, socket_dir, monkeypatch, speed=None)
    await runner.start()
    await runner.apply_settings(None, None, None, "priority")
    await runner.apply_settings(None, None, None, None)
    await runner.apply_settings(None, None, "low", UNSET)
    assert harness.calls("thread/settings/update") == [
        {"threadId": THREAD, "serviceTier": "priority"},
        {"threadId": THREAD, "serviceTier": None},
        {"threadId": THREAD, "effort": "low"},
    ]
    await close(harness)


async def test_the_settings_notification_publishes_the_tier_as_meta(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, harness = await daemon_session(tmp_path, socket_dir, monkeypatch, speed=None)
    await runner.start()
    await runner.notification(
        "thread/settings/updated",
        {
            "threadId": THREAD,
            "threadSettings": {
                "model": "gpt-5.4-codex",
                "effort": "medium",
                "approvalPolicy": "on-request",
                "serviceTier": "priority",
            },
        },
    )
    assert [event["speed"] for event in harness.events("meta") if "speed" in event] == ["priority"]
    # Codex spells a cleared tier `default`, which is the standard speed.
    await runner.notification(
        "thread/settings/updated",
        {"threadId": THREAD, "threadSettings": {"serviceTier": "default"}},
    )
    assert [event["speed"] for event in harness.events("meta") if "speed" in event] == [
        "priority",
        None,
    ]
    validator = event_validator()
    if validator is not None:
        for event in harness.events("meta"):
            validator.validate(event)
    await close(harness)


def test_what_the_device_advertises_and_stores_matches_the_schema() -> None:
    """A `speeds` list and a null `speed` are what the frozen objects allow (A21)."""
    agent = object_validator("AgentInfo")
    session = object_validator("Session")
    if agent is None or session is None:
        pytest.skip("the protocol schema is not present")
    info = AgentInfo(
        agent="codex",
        available=True,
        models=[Choice("gpt-5.4-codex", "GPT-5.4 Codex")],
        speeds=parse_catalog(CATALOGUE).speeds,
        capabilities=["interrupt"],
    )
    agent.validate(info.to_dict())
    row = Session(
        session_id="sess-1",
        device_id="5c2f0a1e-6b3d-4f8a-9c17-0d4e2b6a7f31",
        agent="codex",
        cwd="/repo",
    )
    session.validate(row.to_dict())
    row.speed = "priority"
    session.validate(row.to_dict())


async def test_claude_refuses_the_standard_tier_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent with no tiers has no standard tier to be set back to either."""
    hub, registry = build_hub(tmp_path, monkeypatch)
    add_session(hub, "claude", "default")
    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "speed": None})
    assert caught.value.code == "unsupported"
    registry.close()


async def test_a_tier_the_user_configured_is_adopted_not_cleared(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread born fast because of the user's Codex configuration says so."""
    runner, harness = await daemon_session(
        tmp_path, socket_dir, monkeypatch, speed=None, started_tier="priority"
    )
    await runner.start()
    assert harness.calls("thread/settings/update") == []
    assert [event["speed"] for event in harness.events("meta") if "speed" in event] == ["priority"]
    await close(harness)
