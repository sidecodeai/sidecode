"""Amendment A22: the build a client runs, and `rc-client self-update`."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from rc_client import update
from rc_client.agents.pi import paths as pi_paths
from rc_client.build import as_digest, build_path, digest_of, read_build, write_build
from rc_client.channel import shellrc
from rc_client.config import Config, ensure_dirs, save_config
from rc_client.errors import RcError
from rc_client.service import manager
from rc_client.update import log_tail, self_update, update_log_path

WHEEL_BYTES = b"PK\x03\x04 pretend this is a wheel"
WHEEL_DIGEST = hashlib.sha256(WHEEL_BYTES).hexdigest()
WHEEL_FILENAME = "rc_client-0.1.1-py3-none-any.whl"
GATEWAY = "https://rc.example.com"


class FakeProcess:
    def __init__(self, code: int) -> None:
        self.code = code

    async def wait(self) -> int:
        return self.code


class FakeStream:
    """What `httpx.AsyncClient.stream` returns: an async context manager."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aenter__(self) -> httpx.Response:
        return self.response

    async def __aexit__(self, *exc: object) -> bool:
        return False


def serve(
    payload: bytes = WHEEL_BYTES,
    *,
    filename: str | None = WHEEL_FILENAME,
    status: int = 200,
) -> Any:
    """A stand-in for the gateway's `/dist/rc_client-latest.whl`."""

    def stream(self: Any, method: str, url: str, **kwargs: Any) -> FakeStream:
        headers = {}
        if filename is not None:
            headers["content-disposition"] = f'attachment; filename="{filename}"'
        return FakeStream(
            httpx.Response(
                status,
                content=payload,
                headers=headers,
                request=httpx.Request(method, url),
            )
        )

    return stream


@pytest.fixture
def enrolled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """An enrolled device whose subprocesses are captured instead of run."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    save_config(
        Config(
            gateway_origin=GATEWAY,
            device_id="dev-1",
            device_token="tok-1",
            name="laptop",
        )
    )
    monkeypatch.setattr(shutil, "which", lambda name: f"/fake/{name}")
    commands: list[list[str]] = []

    async def spawn(*command: str, **kwargs: Any) -> FakeProcess:
        commands.append(list(command))
        return FakeProcess(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return commands


# ------------------------------------------------------------- the build file


def test_the_build_file_round_trips() -> None:
    write_build(WHEEL_DIGEST)
    assert read_build() == WHEEL_DIGEST
    assert build_path().name == "client-build"


def test_a_missing_build_file_reads_as_no_build() -> None:
    assert read_build() is None


def test_junk_in_the_build_file_reads_as_no_build() -> None:
    ensure_dirs()
    build_path().write_text("not a digest\n", encoding="utf-8")
    assert read_build() is None


def test_only_a_sha256_hex_digest_is_a_build() -> None:
    assert as_digest(f"  {WHEEL_DIGEST.upper()}\n") == WHEEL_DIGEST
    assert as_digest("deadbeef") is None
    assert as_digest("") is None


def test_a_files_digest_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "wheel"
    target.write_bytes(WHEEL_BYTES)
    assert digest_of(target) == WHEEL_DIGEST


# ------------------------------------------------------------- the downloader


def test_the_wheel_keeps_the_name_the_gateway_gives_it() -> None:
    assert update._wheel_name(f'attachment; filename="{WHEEL_FILENAME}"') == WHEEL_FILENAME


def test_a_wheel_name_that_escapes_the_directory_is_refused() -> None:
    for disposition in (None, "attachment", 'attachment; filename="../../etc/passwd"'):
        with pytest.raises(RcError):
            update._wheel_name(disposition)


# ------------------------------------------------------------- the update pass


async def test_a_wheel_that_is_not_the_requested_build_installs_nothing(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update("c" * 64) is False
    assert enrolled == []
    assert read_build() is None
    assert "not the requested build" in capsys.readouterr().err


async def test_the_update_installs_the_wheel_and_restarts_the_service(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # launchd: `service install` boots the job out and back in, so it is the restart.
    monkeypatch.setattr(manager, "install_restarts", lambda: True)
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True

    install, service, shim = enrolled
    assert install[:4] == ["/fake/uv", "pip", "install", "--python"]
    assert install[-1].endswith(WHEEL_FILENAME)
    assert service[1:] == ["service", "install"]
    assert shim[1:] == ["shim", "install", "--no-shell-rc"]
    assert read_build() == WHEEL_DIGEST


async def test_where_install_does_not_restart_the_update_restarts_last(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """systemd: `install` only rewrites the unit, and the old process kept running.

    The restart is the last step, after the shim and the pi refresh, because on
    systemd it takes the updater with it; and it comes after the log's
    "updated to" line for the same reason.
    """
    monkeypatch.setattr(manager, "install_restarts", lambda: False)
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True

    assert [command[1:] for command in enrolled[1:]] == [
        ["service", "install"],
        ["shim", "install", "--no-shell-rc"],
        ["service", "restart"],
    ]
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-2].startswith("updated to ")
    assert lines[-1].endswith("service restart")


async def test_the_update_keeps_the_shell_startup_file_the_installer_wrote(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    shellrc.add(shellrc.rc_file())
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True
    assert enrolled[2][1:] == ["shim", "install"]


async def test_a_gateway_error_leaves_the_build_alone(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve(status=503))
    with pytest.raises(RcError) as caught:
        await self_update(WHEEL_DIGEST)
    assert "503" in caught.value.message
    assert enrolled == []
    assert read_build() is None


async def test_a_build_that_is_not_a_digest_is_refused(enrolled: list[list[str]]) -> None:
    with pytest.raises(RcError) as caught:
        await self_update("nonsense")
    assert caught.value.code == "bad_request"


async def test_the_update_leaves_no_working_directory_behind(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True
    assert list(build_path().parent.glob("update-*")) == []


# ------------------------------------------------------- what the wheel ships


def install_pi_extension() -> Path:
    """An older copy of the extension where pi loads it from."""
    target = pi_paths.installed_extension()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// an older build\n", encoding="utf-8")
    return target


def fail_on(enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    """Make exactly one of the update's steps exit non-zero."""

    async def spawn(*command: str, **kwargs: Any) -> FakeProcess:
        enrolled.append(list(command))
        return FakeProcess(1 if tuple(command[1:]) == arguments else 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)


async def test_the_update_refreshes_an_installed_pi_extension(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_pi_extension()
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True
    assert [command[1:] for command in enrolled][1:] == [
        ["service", "install"],
        ["shim", "install", "--no-shell-rc"],
        ["pi", "setup"],
    ]


async def test_the_update_leaves_a_device_without_the_pi_extension_alone(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True
    assert [command[1:] for command in enrolled][1:] == [
        ["service", "install"],
        ["shim", "install", "--no-shell-rc"],
    ]
    assert "pi's extension is not installed" in capsys.readouterr().out
    assert not pi_paths.installed_extension().exists()


async def test_a_pi_extension_that_cannot_be_refreshed_still_leaves_the_update_done(
    enrolled: list[list[str]], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    install_pi_extension()
    fail_on(enrolled, monkeypatch, "pi", "setup")
    monkeypatch.setattr(httpx.AsyncClient, "stream", serve())
    assert await self_update(WHEEL_DIGEST) is True
    assert read_build() == WHEEL_DIGEST
    assert "pi extension" in capsys.readouterr().err


# ----------------------------------------------------------------- the log


def test_the_log_tail_is_the_last_line_that_says_something() -> None:
    ensure_dirs()
    update_log_path().write_text("downloading\n\nerror: uv exited with 2\n\n", encoding="utf-8")
    assert log_tail() == "error: uv exited with 2"


def test_an_absent_log_still_yields_a_message() -> None:
    assert log_tail() == "the update failed and left no log"


async def test_the_spawned_updater_writes_into_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update, "rc_client_executable", lambda: "/usr/bin/true")
    process = await update.spawn_self_update(WHEEL_DIGEST)
    assert await process.wait() == 0
    assert "rc-client self-update" in update_log_path().read_text(encoding="utf-8")
