"""What the device advertises for Grok Build, from the files the CLI keeps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rc_client.agents.grok import catalog as grok_catalog
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok.plugin import detect
from rc_client.agents.registry import DetectContext
from tests.helpers import load_fixture, object_validator
from tests.test_discovery import fake_binary

MODELS_CACHE = {
    "models": {
        "grok-4.6": {
            "info": {
                "id": "grok-4.6",
                "name": "Grok 4.6",
                "context_window": 500000,
                "supports_reasoning_effort": True,
                "reasoning_effort": "high",
                "reasoning_efforts": [
                    {"id": "xhigh", "label": "Extra High Effort", "default": False},
                    {"id": "high", "label": "High Effort", "default": True},
                    {"id": "medium", "label": "Medium Effort", "default": False},
                    {"id": "low", "label": "Low Effort", "default": False},
                ],
            }
        },
        "grok-4.5": {
            "info": {
                "id": "grok-4.5",
                "name": "Grok 4.5",
                "context_window": 500000,
                "supports_reasoning_effort": True,
                "reasoning_efforts": [
                    {"id": "high", "default": True},
                    {"id": "medium", "default": False},
                    {"id": "low", "default": False},
                ],
            }
        },
    }
}


@pytest.fixture
def grok_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "grok"
    home.mkdir()
    monkeypatch.setattr(grok_runtime, "home", lambda: home)
    # The absolute fallbacks (`~/.local/bin/grok` and friends) are not on PATH,
    # so the real home has to move as well or a test would find the real agent.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("RC_GROK_BIN", raising=False)
    return home


def write_cache(home: Path) -> None:
    (home / "models_cache.json").write_text(json.dumps(MODELS_CACHE), encoding="utf-8")


async def test_grok_is_unavailable_when_nothing_is_installed(
    grok_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(grok_dir / "empty"))
    info = await detect(DetectContext())
    assert info.available is False
    assert info.path is None
    assert info.version is None
    # An agent that is not installed still says what it would offer.
    assert [choice.id for choice in info.models] == ["grok-4.6", "grok-4.5"]


async def test_the_shipped_launcher_is_preferred_over_path(
    grok_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = fake_binary(grok_dir / "bin", "agent", "grok 1.0.25 (f7e67d6988e2)")
    on_path = fake_binary(grok_dir / "elsewhere", "grok", "grok 0.9.0")
    monkeypatch.setenv("PATH", str(on_path.parent))
    info = await detect(DetectContext())
    assert info.path == str(shipped)
    assert info.version == "1.0.25"
    assert info.available is True


async def test_an_explicit_override_wins(grok_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_binary(grok_dir / "bin", "agent", "grok 1.0.25")
    override = fake_binary(grok_dir / "override", "agent", "grok 9.9.9")
    monkeypatch.setenv("RC_GROK_BIN", str(override))
    assert grok_runtime.resolve_binary() == str(override)


async def test_models_and_efforts_come_from_the_cache_and_the_config(grok_dir: Path) -> None:
    write_cache(grok_dir)
    (grok_dir / "config.toml").write_text(
        '[models]\ndefault = "grok-4.5"\ndefault_reasoning_effort = "xhigh"\n', encoding="utf-8"
    )
    info = await detect(DetectContext())
    assert [choice.id for choice in info.models] == ["grok-4.6", "grok-4.5"]
    # The union, weakest first, not per model.
    assert [choice.id for choice in info.efforts] == ["low", "medium", "high", "xhigh"]
    assert info.default_model == "grok-4.5"
    assert info.default_effort == "xhigh"


async def test_without_a_config_the_defaults_are_the_catalogue_s_own(grok_dir: Path) -> None:
    write_cache(grok_dir)
    info = await detect(DetectContext())
    assert info.default_model == "grok-4.6"
    assert info.default_effort == "high"


def test_an_effort_the_model_does_not_offer_is_dropped(grok_dir: Path) -> None:
    write_cache(grok_dir)
    catalog = grok_catalog.load()
    assert catalog.clamp_effort("grok-4.5", "xhigh") is None
    assert catalog.clamp_effort("grok-4.6", "xhigh") == "xhigh"
    assert catalog.context_window("grok-4.6") == 500000


async def test_what_grok_advertises_matches_the_protocol_fixture(grok_dir: Path) -> None:
    """`fixtures/objects/agent.grok.json` is the contract for this agent."""
    write_cache(grok_dir)
    binary = fake_binary(grok_dir / "bin", "agent", "grok 1.0.30 (f7e67d6988e2)")
    (grok_dir / "config.toml").write_text("[cli]\nuse_leader = true\n", encoding="utf-8")
    info = await detect(DetectContext())
    expected = load_fixture("objects/agent.grok.json")
    payload = info.to_dict()
    validator = object_validator("AgentInfo")
    if validator is not None:
        validator.validate(payload)
    assert payload["version"] == expected["version"]
    assert payload["models"] == expected["models"]
    assert payload["default_model"] == expected["default_model"]
    assert payload["permission_modes"] == expected["permission_modes"]
    assert payload["efforts"] == expected["efforts"]
    assert payload["default_effort"] == expected["default_effort"]
    assert payload["capabilities"] == expected["capabilities"]
    # Grok Build attaches through its leader (A28); `attach_ready` is the
    # person's own configuration, because the leader has nothing to hand-shake
    # with until some client has started it.
    assert payload["attach"] == expected["attach"] == "leader"
    assert payload["attach_ready"] is True
    assert payload["shared_interrupt"] is True
    assert payload["shared_settings"] is True
    assert payload["shared_attachments"] is False
    assert payload["speeds"] == []
    assert payload["path"] == str(binary)


async def test_attach_ready_is_off_until_the_leader_flag_is_set(grok_dir: Path) -> None:
    """`use_leader` is off by default, so the next `grok` runs its own agent."""
    write_cache(grok_dir)
    fake_binary(grok_dir / "bin", "agent", "grok 1.0.30")
    info = await detect(DetectContext())
    assert info.attach == "leader"
    assert info.attach_ready is False

    (grok_dir / "config.toml").write_text(
        '[cli]\nuse_leader = true\n\n[sandbox]\nprofile = "workspace"\n', encoding="utf-8"
    )
    # A sandbox profile refuses leader mode however the flag is set.
    assert (await detect(DetectContext())).attach_ready is False
