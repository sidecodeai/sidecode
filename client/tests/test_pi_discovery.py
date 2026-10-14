"""What the device advertises for pi, and where each part of it comes from.

`tests/fixtures/pi/list-models.txt` was recorded from `pi --list-models` on an
install (0.85.1) with several providers logged in. Detection runs against a
shim, never the real binary: `PATH`, `$HOME` and the system prefixes are all
redirected, because this machine does have a pi and finding it would make the
results depend on whoever is logged in to it.
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from rc_client.agents.pi import catalog as pi_catalog
from rc_client.agents.pi import install as pi_install
from rc_client.agents.pi import paths as pi_paths
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.agents.pi.plugin import detect
from rc_client.agents.registry import DetectContext
from tests.helpers import load_fixture, object_validator

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pi"
NO_MODELS = "No models available. Use /login to log into a provider via OAuth or API key."


def fake_pi(directory: Path, version: str, models: str) -> Path:
    """An executable that answers `--version` and `--list-models` like pi.

    Detection runs with `PATH` pointing at nothing but this directory, so the
    shim can only use absolute paths and shell builtins.
    """
    directory.mkdir(parents=True, exist_ok=True)
    models_file = directory / "models.txt"
    models_file.write_text(models, encoding="utf-8")
    answers = directory / "answers.py"
    answers.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "argument = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        f"if argument == '--version':\n    print({version!r})\n"
        "elif argument == '--list-models':\n"
        f"    sys.stdout.write(Path({str(models_file)!r}).read_text())\n",
        encoding="utf-8",
    )
    path = directory / "pi"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{answers}" "$@"\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture(autouse=True)
def pi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "pi"
    (home / "agent").mkdir(parents=True)
    monkeypatch.setattr(pi_runtime, "home", lambda: home)
    # The absolute fallbacks (`~/.local/bin/pi` and friends) are not on PATH, so
    # the real home has to move as well or a test could find a real pi, and the
    # system prefixes have to go with it.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(pi_runtime, "SYSTEM_PREFIXES", ())
    monkeypatch.delenv("RC_PI_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    pi_catalog.model_cache.forget()
    yield home
    pi_catalog.model_cache.forget()


def write_settings(home: Path, settings: dict[str, object]) -> None:
    (home / "agent" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


async def test_pi_is_unavailable_when_nothing_is_installed() -> None:
    info = await detect(DetectContext())
    assert info.available is False
    assert info.path is None
    assert info.version is None
    # An agent that is not installed still says what it would offer.
    assert [choice.id for choice in info.models] == [
        "anthropic/claude-sonnet-4-5",
        "openai/gpt-5",
    ]


async def test_the_binary_on_path_is_found_and_its_version_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = fake_pi(tmp_path / "bin", "0.85.1", NO_MODELS)
    monkeypatch.setenv("PATH", str(binary.parent))
    info = await detect(DetectContext())
    assert info.available is True
    assert info.path == str(binary)
    assert info.version == "0.85.1"


async def test_an_explicit_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    on_path = fake_pi(tmp_path / "bin", "0.85.1", NO_MODELS)
    override = fake_pi(tmp_path / "override", "9.9.9", NO_MODELS)
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("RC_PI_BIN", str(override))
    assert pi_runtime.resolve_binary() == str(override)


async def test_models_come_from_the_recorded_list_models_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = (FIXTURES / "list-models.txt").read_text(encoding="utf-8")
    binary = fake_pi(tmp_path / "bin", "0.85.1", table)
    monkeypatch.setenv("PATH", str(binary.parent))
    info = await detect(DetectContext())
    ids = [choice.id for choice in info.models]
    assert ids[0] == "anthropic/claude-fable-5"
    assert "openai/gpt-5" in ids
    # Every provider the person is logged in to is offered, not just one.
    assert {choice.id.split("/")[0] for choice in info.models} >= {
        "anthropic",
        "google",
        "openai",
    }
    # The first row is the default until a person saves one of their own.
    assert info.default_model == "anthropic/claude-fable-5"
    labels = {choice.id: choice.label for choice in info.models}
    assert labels["anthropic/claude-sonnet-4-5"] == "Claude Sonnet 4.5"
    assert labels["openai/gpt-5"] == "GPT-5"


def test_the_header_and_the_no_models_sentence_are_not_models() -> None:
    table = (FIXTURES / "list-models.txt").read_text(encoding="utf-8")
    models = pi_catalog.parse_models(table)
    assert len(models) == 75
    assert all(choice.id != "provider/model" for choice in models)
    # Nothing logged in: pi prints a sentence where the table would be.
    assert pi_catalog.parse_models(NO_MODELS) == []


def test_a_model_id_reads_as_its_own_name() -> None:
    assert pi_catalog.label_for("claude-sonnet-4-5") == "Claude Sonnet 4.5"
    assert pi_catalog.label_for("gpt-5") == "GPT-5"
    assert pi_catalog.label_for("gpt-4.1-nano") == "GPT-4.1 Nano"
    assert pi_catalog.label_for("o3-pro") == "O3 Pro"


async def test_a_person_s_own_defaults_win(pi_home: Path) -> None:
    write_settings(
        pi_home,
        {
            "defaultProvider": "openai",
            "defaultModel": "gpt-5",
            "defaultThinkingLevel": "xhigh",
        },
    )
    info = await detect(DetectContext())
    assert info.default_model == "openai/gpt-5"
    assert info.default_effort == "xhigh"
    # A level pi exposes only for some models is advertised when it is the one
    # in force, so the app can name what the session is running at.
    assert [choice.id for choice in info.efforts] == ["off", "low", "medium", "high", "xhigh"]


async def test_a_meaningless_thinking_level_is_ignored(pi_home: Path) -> None:
    write_settings(pi_home, {"defaultThinkingLevel": "ludicrous"})
    info = await detect(DetectContext())
    assert info.default_effort == "medium"
    assert [choice.id for choice in info.efforts] == ["off", "low", "medium", "high"]


async def test_what_pi_advertises_matches_the_protocol_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fixtures/objects/agent.pi.json` is the contract for this agent."""
    binary = fake_pi(tmp_path / "bin", "0.85.1", NO_MODELS)
    monkeypatch.setenv("PATH", str(binary.parent))
    info = await detect(DetectContext())
    expected = load_fixture("objects/agent.pi.json")
    payload = info.to_dict()
    validator = object_validator("AgentInfo")
    if validator is not None:
        validator.validate(payload)
    assert payload["version"] == expected["version"]
    assert payload["models"] == expected["models"]
    assert payload["default_model"] == expected["default_model"]
    assert payload["efforts"] == expected["efforts"]
    assert payload["default_effort"] == expected["default_effort"]
    assert payload["capabilities"] == expected["capabilities"]
    # A26: the modes are the device's own, enforced by its extension.
    assert payload["permission_modes"] == expected["permission_modes"]
    assert payload["default_permission_mode"] == expected["default_permission_mode"]
    assert payload["attach"] == "extension"
    assert payload["shared_interrupt"] is True
    assert payload["shared_settings"] is True
    assert payload["shared_attachments"] is True
    assert payload["speeds"] == []
    assert payload["path"] == str(binary)


async def test_attach_ready_follows_the_installed_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pi_home: Path
) -> None:
    """`attach_ready` is the extension being there, at this build, and nothing else."""
    binary = fake_pi(tmp_path / "bin", "0.85.1", NO_MODELS)
    monkeypatch.setenv("PATH", str(binary.parent))
    assert (await detect(DetectContext())).attach_ready is False
    pi_install.install()
    assert (await detect(DetectContext())).attach_ready is True
    # A copy from an older build is not the one this device would load.
    pi_paths.installed_extension().write_text("// stale\n", encoding="utf-8")
    assert (await detect(DetectContext())).attach_ready is False
