"""The `claude` shim, its MCP config and the shell startup block."""

from __future__ import annotations

import json
import os
import pty
import stat
import subprocess
from pathlib import Path

import pytest

from rc_client.channel import paths, shellrc, shim
from rc_client.channel.mcp_config import mcp_config, write_mcp_config
from rc_client.channel.settings import write_settings


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], True),
        (["--resume"], True),
        (["--model", "opus"], True),
        (["-p", "hello"], False),
        (["--print"], False),
        (["--print=hi"], False),
        (["--input-format", "stream-json"], False),
        (["--output-format=stream-json"], False),
        (["--sdk-url", "ws://127.0.0.1:1"], False),
        (["--mcp-config", "/tmp/x.json"], False),
        (["--dangerously-load-development-channels", "server:rc"], False),
    ],
)
def test_the_shim_only_adds_channel_flags_to_a_plain_interactive_run(
    argv: list[str], expected: bool
) -> None:
    assert shim.should_attach(argv, stdin_tty=True, stdout_tty=True) is expected


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"), [(False, True), (True, False), (False, False)]
)
def test_a_piped_invocation_always_passes_through(stdin_tty: bool, stdout_tty: bool) -> None:
    assert shim.should_attach([], stdin_tty=stdin_tty, stdout_tty=stdout_tty) is False


def _fake_claude(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "claude"
    target.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    target.chmod(0o755)
    return target


def test_install_writes_a_shim_that_is_recognisable_and_removable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _fake_claude(tmp_path / "real")
    monkeypatch.setenv("PATH", str(real.parent))
    state = shim.install()

    assert state.installed
    assert state.real == str(real)
    assert shim.is_shim(paths.shim_path())
    assert not shim.is_shim(real)
    assert json.loads(paths.mcp_config_path().read_text())["mcpServers"]["rc"]["args"][-1] == (
        "channel"
    )
    assert paths.settings_path().is_file()

    removal = shim.remove()
    assert (removal.shim, removal.settings) == (True, True)
    assert not paths.settings_path().exists()
    assert shim.remove() == shim.ShimRemoval(shim=False, settings=False)


def test_remove_leaves_a_foreign_executable_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    paths.bin_dir().mkdir(parents=True, exist_ok=True)
    paths.shim_path().write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")
    assert shim.remove().shim is False
    assert paths.shim_path().exists()


def test_the_shim_resolves_past_itself_and_appends_the_flags_only_on_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real script: a pipe must reach the wrapped binary untouched."""
    real = _fake_claude(tmp_path / "real")
    monkeypatch.setenv("PATH", f"{paths.bin_dir()}{os.pathsep}{real.parent}")
    shim.install()
    write_mcp_config()

    result = subprocess.run(
        [str(paths.shim_path()), "--resume", "abc"],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.stdout.splitlines() == ["--resume", "abc"]


def _run_shim_on_a_tty(args: list[str]) -> list[str]:
    """Run the wrapper with both ends on a pty: what makes it append its flags.

    The output is read while the child is still alive. A pty discards whatever
    is still queued when its last slave closes, so collecting it afterwards
    reads an empty terminal.
    """
    primary, secondary = pty.openpty()
    chunks: list[bytes] = []
    try:
        process = subprocess.Popen(
            [str(paths.shim_path()), *args],
            stdin=secondary,
            stdout=secondary,
            stderr=subprocess.DEVNULL,
        )
        os.close(secondary)
        while True:
            try:
                data = os.read(primary, 65536)
            except OSError:  # the child exited and closed the last slave
                break
            if not data:
                break
            chunks.append(data)
        assert process.wait() == 0
    finally:
        os.close(primary)
    text = b"".join(chunks).decode("utf-8").replace("\r\n", "\n")
    return [line for line in text.split("\n") if line]


def _installed_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = _fake_claude(tmp_path / "real")
    monkeypatch.setenv("PATH", f"{paths.bin_dir()}{os.pathsep}{real.parent}")
    shim.install()


def test_an_interactive_run_gets_the_channel_flags_and_the_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed_shim(tmp_path, monkeypatch)
    assert _run_shim_on_a_tty(["--model", "opus"]) == [
        "--model",
        "opus",
        shim.CHANNEL_FLAG,
        shim.CHANNEL_VALUE,
        "--mcp-config",
        str(paths.mcp_config_path()),
        "--settings",
        str(paths.settings_path()),
    ]


@pytest.mark.parametrize(
    "own",
    [["--settings", "/tmp/mine.json"], ["--settings=/tmp/mine.json"]],
    ids=["space", "equals"],
)
def test_a_settings_file_of_the_persons_own_is_kept_and_never_doubled(
    own: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their settings win; the session still attaches, it just carries no hook."""
    _installed_shim(tmp_path, monkeypatch)
    tokens = _run_shim_on_a_tty(own)
    assert tokens[: len(own)] == own
    assert str(paths.settings_path()) not in tokens
    assert tokens.count("--settings") == own.count("--settings")
    assert shim.CHANNEL_FLAG in tokens


def test_no_settings_file_means_no_settings_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed_shim(tmp_path, monkeypatch)
    paths.settings_path().unlink()
    tokens = _run_shim_on_a_tty([])
    assert "--settings" not in tokens
    assert tokens[-2:] == ["--mcp-config", str(paths.mcp_config_path())]


def test_a_shim_earlier_on_path_is_skipped_when_resolving_the_real_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _fake_claude(tmp_path / "real")
    monkeypatch.setenv("PATH", f"{paths.bin_dir()}{os.pathsep}{real.parent}")
    shim.install()
    assert shim.real_claude() == str(real)
    assert shim.status().on_path is True


def test_the_mcp_config_names_this_installation_and_its_home() -> None:
    payload = mcp_config(["/opt/rc/bin/rc-client", "channel"])
    server = payload["mcpServers"]["rc"]
    assert server["command"] == "/opt/rc/bin/rc-client"
    assert server["args"] == ["channel"]
    assert server["env"]["RC_CLIENT_HOME"]


def test_the_settings_file_holds_both_hooks_and_stays_private() -> None:
    target = write_settings(
        ["/opt/rc/bin/rc-client", "hook", "session-start"],
        ["/opt/rc/bin/rc-client", "hook", "permission-request"],
    )
    assert target == paths.settings_path()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    hooks = json.loads(target.read_text())["hooks"]
    entries = hooks["SessionStart"]
    assert len(entries) == 1
    assert "matcher" not in entries[0], "no matcher, so every source fires"
    assert entries[0]["hooks"][0]["command"].endswith("/opt/rc/bin/rc-client hook session-start")

    questions = hooks["PermissionRequest"]
    assert len(questions) == 1
    assert questions[0]["matcher"] == "AskUserQuestion"
    assert questions[0]["hooks"][0]["command"].endswith(
        "/opt/rc/bin/rc-client hook permission-request"
    )


def test_the_socket_path_stays_inside_the_unix_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RC_CLIENT_HOME", "/tmp/" + "d" * 120)
    assert len(str(paths.socket_path()).encode("utf-8")) < 104


def _symlinked_rc(tmp_path: Path) -> tuple[Path, Path]:
    """A startup file reached through a symlink, as a dotfile repository would."""
    real = tmp_path / "store" / "zshrc"
    real.parent.mkdir(parents=True)
    real.write_text("export EDITOR=vim\n", encoding="utf-8")
    link = tmp_path / ".zshrc"
    link.symlink_to(real)
    return link, real


def test_the_shell_block_is_added_once_and_removed_by_its_markers(tmp_path: Path) -> None:
    link, real = _symlinked_rc(tmp_path)

    assert shellrc.add(link) is True
    assert shellrc.add(link) is False
    after_add = link.read_text(encoding="utf-8")
    assert after_add.count(shellrc.BEGIN) == 1
    assert str(paths.bin_dir()) in after_add
    assert link.is_symlink(), "the startup file must stay a symlink"
    assert real.read_text(encoding="utf-8") == after_add

    assert shellrc.remove(link) is True
    assert shellrc.remove(link) is False
    assert link.read_text(encoding="utf-8") == "export EDITOR=vim\n"
    assert link.is_symlink()


def test_the_shell_block_refreshes_in_place_when_the_path_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / ".bashrc"
    target.write_text("# mine\n", encoding="utf-8")
    shellrc.add(target)
    monkeypatch.setenv("RC_CLIENT_HOME", str(tmp_path / "elsewhere"))
    assert shellrc.add(target) is True
    text = target.read_text(encoding="utf-8")
    assert text.count(shellrc.BEGIN) == 1
    assert str(tmp_path / "elsewhere" / "bin") in text
    assert text.startswith("# mine\n")


def test_the_startup_file_follows_the_login_shell(tmp_path: Path) -> None:
    assert shellrc.rc_file("/bin/zsh", tmp_path).name == ".zshrc"
    assert shellrc.rc_file("/usr/bin/bash", tmp_path).name == ".bashrc"
    assert shellrc.rc_file("/usr/bin/fish", tmp_path).name == ".profile"


def test_the_service_environment_puts_the_shim_first_exactly_once() -> None:
    from rc_client.service import launchd, systemd

    combined = paths.path_with_shim(f"/usr/bin{os.pathsep}{paths.bin_dir()}{os.pathsep}/bin")
    assert combined.split(os.pathsep) == [str(paths.bin_dir()), "/usr/bin", "/bin"]
    assert str(paths.bin_dir()) in launchd.render("/opt/rc/bin/rc-client")
    assert f"Environment=PATH={paths.bin_dir()}" in systemd.render("/opt/rc/bin/rc-client")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["claude"], True),
        (["claude", "--resume", "abc"], True),
        (
            [
                "claude",
                "--dangerously-load-development-channels",
                "server:rc",
                "--mcp-config",
                "/home/me/.rc-client/state/claude-mcp.json",
            ],
            True,
        ),
        (["claude", "mcp", "serve"], False),
        (["claude", "--bg-pty-host"], False),
        (["claude", "--daemon=1"], False),
    ],
)
def test_a_session_started_through_the_shim_still_counts_as_a_terminal_holder(
    argv: list[str], expected: bool
) -> None:
    """The shim's `--mcp-config` must not read as a background helper process."""
    from rc_client.agents.claude.holders import _looks_like_claude
    from rc_client.procscan import Proc

    proc = Proc(pid=1, ppid=0, start="0", command=" ".join(argv))
    assert _looks_like_claude(proc) is expected
