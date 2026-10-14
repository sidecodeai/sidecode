"""Amendment A11: bringing the shared Codex daemon up, and supervising it."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from rc_client.agents.codex import runtime
from rc_client.agents.codex.daemon import control, setup
from rc_client.agents.codex.runtime import CODEX_HOME
from rc_client.config import client_home
from rc_client.service import codex as supervision

# The real thing, from `codex app-server daemon version` on a healthy machine.
REAL_VERSION = (
    '{"status":"running","backend":"pid","managedCodexPath":'
    '"/Users/u/.codex/packages/standalone/current/bin/codex","managedCodexVersion":"0.154.0",'
    '"socketPath":"/Users/u/.codex/app-server-control/app-server-control.sock",'
    '"cliVersion":"0.154.0","appServerVersion":"0.154.0"}'
)


def test_a_codex_on_path_does_not_count_as_a_standalone_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The npm and Homebrew builds cannot bootstrap the daemon, so they are not "installed"."""
    npm_bin = tmp_path / "npm"
    npm_bin.mkdir()
    fake = npm_bin / "codex"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(npm_bin))
    monkeypatch.setattr(setup, "STANDALONE", tmp_path / "absent/bin/codex")
    assert setup.standalone_binary() is None


def test_the_standalone_binary_is_the_one_the_daemon_commands_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    standalone = tmp_path / "packages/standalone/current/bin/codex"
    standalone.parent.mkdir(parents=True)
    standalone.write_text("#!/bin/sh\n", encoding="utf-8")
    standalone.chmod(0o755)
    monkeypatch.setattr(setup, "STANDALONE", standalone)
    assert setup.standalone_binary() == str(standalone)
    standalone.chmod(0o644)
    assert setup.standalone_binary() is None


def test_on_path_compares_resolved_directories(tmp_path: Path) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    assert setup.on_path(directory, f"/usr/bin:{directory}") is True
    assert setup.on_path(directory, "/usr/bin:/bin") is False


def test_a_download_that_is_not_a_script_is_never_executed() -> None:
    assert setup.looks_like_shell_script("#!/bin/sh\necho hi\n") is True
    assert setup.looks_like_shell_script("#!/usr/bin/env bash\n") is True
    assert setup.looks_like_shell_script("<!doctype html><html>") is False
    assert setup.looks_like_shell_script("") is False


# ----------------------------------------------------------------- the installer


def test_the_installer_writes_its_profile_block_into_a_home_of_ours() -> None:
    """Ruling R2: the rewrite happens whatever we do, so it happens somewhere harmless."""
    environment = setup.installer_env()
    assert environment["HOME"] == str(setup.installer_home())
    assert Path(environment["HOME"]).is_dir()
    assert environment["CODEX_NON_INTERACTIVE"] == "1"
    assert environment["CODEX_HOME"] == str(CODEX_HOME)
    assert environment["CODEX_INSTALL_DIR"] == str(setup.LOCAL_BIN)
    assert Path(environment["HOME"]) != Path.home()


def test_the_installer_home_is_inside_the_device_home() -> None:
    assert setup.installer_home().is_relative_to(client_home())


def test_the_path_hint_is_a_line_we_print_not_a_file_we_write() -> None:
    assert setup.PATH_HINT.startswith("Codex is not on your PATH")
    assert f'export PATH="{setup.LOCAL_BIN}:$PATH"' in setup.PATH_HINT


async def test_setup_installs_codex_even_when_local_bin_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R2: the PATH gate never prevented the dotfile rewrite, so it only blocked the fix."""
    standalone = str(tmp_path / "standalone-codex")
    installed: list[bool] = []

    async def fake_install() -> tuple[bool, str]:
        installed.append(True)
        monkeypatch.setattr(setup, "standalone_binary", lambda: standalone)
        return True, "installed"

    async def fake_bootstrap(binary: str) -> tuple[bool, str]:
        return True, "bootstrapped"

    async def fake_status() -> setup.DaemonStatus:
        return setup.DaemonStatus(standalone, standalone, "/s", True, True, "loaded")

    monkeypatch.setattr(setup, "standalone_binary", lambda: None)
    monkeypatch.setattr(setup, "on_path", lambda directory, value=None: False)
    monkeypatch.setattr(setup, "install_codex", fake_install)
    monkeypatch.setattr(control, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(setup, "status", fake_status)
    monkeypatch.setattr(supervision, "install", lambda binary: Path(binary + ".plist"))
    monkeypatch.setattr(supervision, "post_install_hint", lambda: None)

    ok, lines = await setup.setup()
    assert (ok, installed) == (True, [True])
    assert setup.PATH_HINT in lines


async def test_setup_refuses_to_install_when_told_not_to(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "standalone_binary", lambda: None)
    ok, lines = await setup.setup(install_missing=False)
    assert ok is False
    assert "--no-install" in lines[0]


async def test_setup_bootstraps_and_supervises_the_standalone_binary_not_the_one_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    standalone = str(tmp_path / "standalone-codex")
    used: list[str] = []
    downloaded: list[bool] = []

    async def record_bootstrap(binary: str) -> tuple[bool, str]:
        used.append(binary)
        return True, "bootstrapped"

    async def never_install() -> tuple[bool, str]:
        downloaded.append(True)
        return False, "should not run"

    monkeypatch.setattr(setup, "standalone_binary", lambda: standalone)
    monkeypatch.setattr(setup, "install_codex", never_install)
    monkeypatch.setattr(control, "bootstrap", record_bootstrap)
    monkeypatch.setattr(supervision, "install", lambda binary: Path(binary + ".plist"))
    monkeypatch.setattr(supervision, "post_install_hint", lambda: None)

    async def fake_status() -> setup.DaemonStatus:
        return setup.DaemonStatus(standalone, "/opt/homebrew/bin/codex", "/s", True, True, "loaded")

    monkeypatch.setattr(setup, "status", fake_status)
    ok, lines = await setup.setup()
    assert (ok, used, downloaded) == (True, [standalone], [])
    assert any(line.startswith(f"Supervision installed at {standalone}") for line in lines)


# --------------------------------------------------------------------- status


def test_the_status_summary_says_what_to_do_next() -> None:
    healthy = setup.DaemonStatus("/bin/codex", "/bin/codex", "/s.sock", True, True, "loaded")
    assert healthy.summary() == "healthy (loaded)"
    assert healthy.healthy is True
    stale = setup.DaemonStatus("/bin/codex", "/bin/codex", "/s.sock", True, False, "not installed")
    assert stale.summary() == "socket present but not answering"
    missing = setup.DaemonStatus(None, None, "/s.sock", False, False, "not installed")
    assert missing.summary() == "not running"
    assert len(missing.lines()) == 6


def test_a_codex_on_path_from_another_build_is_reported_never_warned_about() -> None:
    """Ruling R5: an npm TUI joins the shared daemon like any other, so there is nothing to warn."""
    status = setup.DaemonStatus(
        "/home/u/.codex/packages/standalone/current/bin/codex",
        "/opt/homebrew/bin/codex",
        "/s.sock",
        True,
        True,
        "loaded",
    )
    assert status.summary() == "healthy (loaded)"
    printed = " ".join(status.lines())
    assert "/opt/homebrew/bin/codex" in printed
    assert "warning" not in printed
    assert "uninstall" not in printed


def test_status_prints_both_versions_and_says_a_restart_is_pending() -> None:
    """Ruling R3: `daemon version` is the only place a drift is visible."""
    drifted = setup.DaemonStatus(
        "/bin/codex",
        "/bin/codex",
        "/s.sock",
        True,
        True,
        "loaded",
        control.DaemonVersion("running", app_server="0.153.0", managed="0.154.0", cli="0.154.0"),
    )
    assert drifted.drifted is True
    assert drifted.summary() == "healthy (loaded); drifted; restart pending"
    lines = drifted.lines()
    assert "app-server version  0.153.0" in lines
    assert "installed version   0.154.0 (drifted; restart pending)" in lines
    assert len(lines) == 8


def test_matching_versions_are_printed_without_a_drift_note() -> None:
    current = setup.DaemonStatus(
        "/bin/codex",
        "/bin/codex",
        "/s.sock",
        True,
        True,
        "loaded",
        control.read_version(REAL_VERSION),
    )
    assert current.drifted is False
    assert current.summary() == "healthy (loaded)"
    assert "installed version   0.154.0" in current.lines()


def test_the_real_daemon_version_output_parses() -> None:
    found = control.read_version(REAL_VERSION)
    assert found is not None
    assert (found.status, found.app_server, found.managed, found.cli) == (
        "running",
        "0.154.0",
        "0.154.0",
        "0.154.0",
    )
    assert (found.running, found.drifted) == (True, False)


def test_output_that_is_not_the_daemons_json_is_not_a_version() -> None:
    assert control.read_version("Error: failed to connect to /tmp/app-server-control.sock") is None
    assert control.read_version("") is None
    assert control.read_version("[]") is None


def test_a_daemon_that_never_reported_a_version_is_never_drifted() -> None:
    partial = control.DaemonVersion("running", app_server=None, managed="0.154.0", cli="0.154.0")
    assert partial.drifted is False


# ---------------------------------------------------------------- supervision


def test_the_launchd_agent_runs_daemon_start_under_our_own_label() -> None:
    parsed = plistlib.loads(supervision.render_plist("/opt/codex").encode("utf-8"))
    assert parsed["Label"] == "dev.remote-control.codex-daemon"
    assert parsed["ProgramArguments"] == ["/opt/codex", "app-server", "daemon", "start"]
    assert parsed["RunAtLoad"] is True
    assert parsed["StartInterval"] == supervision.NUDGE_SECONDS
    # Throttling the app-server throttles every Codex round trip through it.
    assert "ProcessType" not in parsed
    # `enable-remote-control` enrols the machine with OpenAI's relay.
    assert "remote-control" not in " ".join(parsed["ProgramArguments"][1:])


def test_the_launchd_agent_escapes_a_path_that_would_break_the_plist() -> None:
    parsed = plistlib.loads(supervision.render_plist("/opt/a&b/codex").encode("utf-8"))
    assert parsed["ProgramArguments"][0] == "/opt/a&b/codex"


def test_the_systemd_unit_nudges_the_idempotent_start_command() -> None:
    unit = supervision.render_unit("/opt/codex")
    assert "ExecStart=/opt/codex app-server daemon start" in unit
    assert f"RestartSec={supervision.NUDGE_SECONDS}" in unit
    assert "WantedBy=default.target" in unit
    assert "--remote-control" not in unit


def test_the_linger_hint_is_linux_only() -> None:
    assert (supervision.post_install_hint() is None) is supervision.IS_MACOS


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_standalone_build_outranks_a_codex_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device-started session has to be the build the shared daemon runs."""
    standalone = _executable(tmp_path / "codex-home/packages/standalone/current/bin/codex")
    on_path = _executable(tmp_path / "npm/codex")
    override = _executable(tmp_path / "override/codex")
    monkeypatch.setattr(runtime, "STANDALONE", standalone)
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.delenv("RC_CODEX_BIN", raising=False)
    assert runtime.resolve_binary() == str(standalone)
    monkeypatch.setenv("RC_CODEX_BIN", str(override))
    assert runtime.resolve_binary() == str(override)


def test_path_is_used_when_no_standalone_build_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions still run without the daemon; only its bootstrap needs the standalone."""
    on_path = _executable(tmp_path / "npm/codex")
    monkeypatch.setattr(runtime, "STANDALONE", tmp_path / "absent/codex")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.delenv("RC_CODEX_BIN", raising=False)
    assert runtime.resolve_binary() == str(on_path)
