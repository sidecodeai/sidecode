"""The pure parts of Grok's leader attachment: the A28 table, readiness, setup."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rc_client.agents.grok import control, cursor, leader
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok import setup as grok_setup
from rc_client.agents.grok.echoes import EchoLog
from rc_client.registry import Registry

SESSION = "01a09b4f-1b25-7e92-b96b-356d292ff2d0"
# Captured before the autouse fixture in `conftest` replaces it, so the one test
# about `GROK_HOME` can exercise the real lookup.
REAL_HOME = grok_runtime.home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "grok"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(grok_runtime, "home", lambda: directory)
    monkeypatch.delenv(leader.SANDBOX_ENV, raising=False)
    return directory


# ------------------------------------------------------------ the A28 table


def test_a_session_a_live_terminal_registered_is_shared() -> None:
    assert control.resolve(False, registered=True, attached=True) == ("terminal", "shared")


def test_a_terminal_the_leader_does_not_hold_stays_terminal() -> None:
    """A `grok` with `use_leader` off runs its own agent; the mirror keeps it."""
    assert control.resolve(False, registered=True, attached=False) == ("terminal", "terminal")


def test_a_session_the_device_created_is_its_own() -> None:
    assert control.resolve(True, registered=False, attached=True) == ("remote", "remote")
    assert control.resolve(True, registered=True, attached=True) == ("remote", "shared")


def test_a_terminal_that_left_leaves_a_resumable_session() -> None:
    assert control.resolve(False, registered=False, attached=True) == ("terminal", "none")
    assert control.resolve(False, registered=False, attached=True, local_turn=True) == (
        "terminal",
        "remote",
    )


def test_a_session_only_disk_knows_is_none() -> None:
    assert control.resolve(False, registered=False, attached=False) == ("terminal", "none")


# ------------------------------------------------------------------ readiness


def test_readiness_is_the_person_s_own_configuration(home: Path) -> None:
    assert leader.config_ready() is False
    (home / "config.toml").write_text("[cli]\nuse_leader = true\n", encoding="utf-8")
    assert leader.use_leader() is True
    assert leader.config_ready() is True


def test_a_sandbox_profile_refuses_the_leader(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "config.toml").write_text(
        '[cli]\nuse_leader = true\n\n[sandbox]\nprofile = "workspace"\n', encoding="utf-8"
    )
    assert leader.sandbox_profile() == "workspace"
    assert leader.config_ready() is False
    (home / "config.toml").write_text(
        '[cli]\nuse_leader = true\n\n[sandbox]\nprofile = "off"\n', encoding="utf-8"
    )
    assert leader.config_ready() is True
    monkeypatch.setenv(leader.SANDBOX_ENV, "strict")
    assert leader.config_ready() is False


def test_an_unreadable_configuration_is_simply_not_ready(home: Path) -> None:
    (home / "config.toml").write_text("[cli\nuse_leader = ", encoding="utf-8")
    assert leader.read_config() == {}
    assert leader.config_ready() is False


def test_the_socket_is_the_one_grok_s_own_commands_look_for(home: Path) -> None:
    assert leader.socket_path() == home / "leader.sock"


def test_grok_home_moves_the_whole_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_runtime, "home", REAL_HOME)
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "elsewhere"))
    assert grok_runtime.home() == tmp_path / "elsewhere"
    assert grok_runtime.config_file() == tmp_path / "elsewhere" / "config.toml"
    monkeypatch.delenv("GROK_HOME")
    assert grok_runtime.home() == Path.home() / ".grok"


# --------------------------------------------------------------- the cursor


def test_the_cursor_is_the_event_id_counter(tmp_path: Path) -> None:
    assert cursor.index_of({"_meta": {"eventId": f"{SESSION}-42"}}) == 42
    assert cursor.index_of({"_meta": {"eventId": "nonsense"}}) == 0
    assert cursor.index_of({}) == 0
    assert cursor.is_replay({"_meta": {"isReplay": True}}) is True
    assert cursor.is_replay({"update": {"isReplay": True}}) is True
    assert cursor.is_replay({"_meta": {"eventId": f"{SESSION}-1"}}) is False


def test_the_mirror_and_the_leader_share_one_mark(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state.sqlite3")
    assert cursor.read(registry, SESSION) == 0
    cursor.write(registry, SESSION, 34)
    assert cursor.read(registry, SESSION) == 34
    assert registry.get_kv(f"grok-cursor:{SESSION}") == "34"


# ---------------------------------------------------------------- the echoes


def test_an_echo_is_claimed_once_and_only_once() -> None:
    echoes = EchoLog()
    echoes.remember("Reply with exactly OK")
    assert echoes.claim("Reply with exactly OK") is True
    # The same words typed at the TUI are somebody else's message.
    assert echoes.claim("Reply with exactly OK") is False
    assert echoes.claim("something else") is False


# ------------------------------------------------------------------- setup


def test_the_flag_is_inserted_under_an_existing_cli_table() -> None:
    before = '# mine\n[cli]\nshow_tips = false\n\n[models]\ndefault = "grok-4.6"\n'
    after = grok_setup.enable(before)
    assert after == (
        '# mine\n[cli]\nuse_leader = true\nshow_tips = false\n\n[models]\ndefault = "grok-4.6"\n'
    )


def test_an_existing_flag_is_replaced_in_place() -> None:
    before = "[cli]\n  use_leader = false  # off for now\nshow_tips = false\n"
    assert grok_setup.enable(before) == "[cli]\n  use_leader = true\nshow_tips = false\n"


def test_a_configuration_with_no_cli_table_gains_one() -> None:
    before = '[models]\ndefault = "grok-4.6"\n'
    assert (
        grok_setup.enable(before) == '[models]\ndefault = "grok-4.6"\n\n[cli]\nuse_leader = true\n'
    )


def test_an_empty_configuration_becomes_the_flag_alone() -> None:
    assert grok_setup.enable("") == "[cli]\nuse_leader = true\n"


def test_a_cli_table_of_its_own_is_not_confused_with_a_sub_table() -> None:
    before = '[cli.theme]\nname = "dark"\n'
    assert grok_setup.enable(before) == '[cli.theme]\nname = "dark"\n\n[cli]\nuse_leader = true\n'


def test_setup_edits_a_symlinked_configuration_in_place(home: Path, tmp_path: Path) -> None:
    """These dotfiles are often symlinks into a synced folder; the inode survives."""
    target = tmp_path / "icloud" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# kept\n[cli]\nshow_tips = false\n", encoding="utf-8")
    inode = target.stat().st_ino
    link = home / "config.toml"
    link.symlink_to(target)

    assert grok_setup.turn_on() is True
    assert link.is_symlink()
    assert target.stat().st_ino == inode
    assert target.read_text(encoding="utf-8") == (
        "# kept\n[cli]\nuse_leader = true\nshow_tips = false\n"
    )
    # Repeating it changes nothing at all.
    assert grok_setup.turn_on() is False
    assert target.stat().st_ino == inode


async def test_status_reports_the_configuration_without_starting_anything(home: Path) -> None:
    (home / "config.toml").write_text("[cli]\nuse_leader = true\n", encoding="utf-8")
    (home / "active_sessions.json").write_text(
        json.dumps([{"session_id": SESSION, "pid": os.getpid(), "cwd": str(home)}]),
        encoding="utf-8",
    )
    found = await grok_setup.status(probe=False)
    assert found.ready is True
    assert found.registered == 1
    assert found.socket_present is False
    lines = "\n".join(found.lines())
    assert "use_leader          on" in lines
    assert "sandbox             off" in lines
    assert grok_setup.RESTART_NOTE in lines
    assert "1 terminal session(s)" in found.summary()


async def test_status_says_what_to_run_when_the_flag_is_off(home: Path) -> None:
    (home / "bin").mkdir(parents=True, exist_ok=True)
    binary = home / "bin" / "agent"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    found = await grok_setup.status(probe=False)
    assert found.ready is False
    assert "rc-client grok setup" in found.summary()
