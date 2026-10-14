"""Terminals (A38): a real pseudo-terminal driven through the fake gateway.

Every shell these tests start is ended by the test that started it, and the
daemon's own shutdown ends whatever is left. The shells run `/bin/sh` in a
scratch home, so no dotfile of the person running the suite is ever read. What a
terminal carries is matched, never printed: each command is typed so that the
terminal's own echo of it cannot be mistaken for the shell's answer.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from rc_client.config import Config, load_config, save_config
from rc_client.daemon import Daemon
from rc_client.errors import RcError
from rc_client.terminal import MAX_TERMINALS, TerminalManager
from rc_client.terminal.shell import MAX_OUTPUT_BYTES
from tests import test_daemon
from tests.helpers import device_frame_validator
from tests.test_gateway_link import FakeGateway

# The whole daemon on a fake gateway, as `test_daemon` builds it.
running_daemon = test_daemon.running_daemon

pytestmark = pytest.mark.skipif(os.name != "posix", reason="a pseudo-terminal needs a POSIX host")

DEVICE = "dev-1"
HOLDER = "app-1"
MAX_INPUT_BYTES = 64 * 1024


@pytest.fixture(autouse=True)
def scratch_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive `/bin/sh` from an empty home, never the user's shell or dotfiles."""
    home = tmp_path / "terminal-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/sh")


def rid() -> str:
    return str(uuid.uuid4())


def validate(frame: dict[str, Any], name: str) -> None:
    """Hold a frame the device produced against the frozen device schema."""
    validator = device_frame_validator(name)
    if validator is not None:
        validator.validate(frame)


def sample_config() -> Config:
    return Config(
        gateway_origin="https://rc.example.com",
        device_id=DEVICE,
        device_token="secret-token",
        name="mac-studio",
    )


async def ask(server: FakeGateway, frame: dict[str, Any]) -> dict[str, Any]:
    return await test_daemon.request(server, {"id": rid(), "device_id": DEVICE, **frame})


async def open_terminal(
    server: FakeGateway, *, holder: str = HOLDER, cols: int = 80, rows: int = 24
) -> str:
    reply = await ask(server, {"type": "terminal.open", "from": holder, "cols": cols, "rows": rows})
    assert reply["ok"] is True
    return str(reply["result"]["terminal_id"])


async def type_line(
    server: FakeGateway, terminal_id: str, line: str, *, holder: str = HOLDER
) -> None:
    data = base64.b64encode(f"{line}\n".encode()).decode("ascii")
    reply = await ask(
        server,
        {"type": "terminal.input", "from": holder, "terminal_id": terminal_id, "data": data},
    )
    assert reply["ok"] is True


async def detach(server: FakeGateway, terminal_id: str) -> None:
    reply = await ask(
        server, {"type": "terminal.detach", "from": "gateway", "terminal_id": terminal_id}
    )
    assert reply["ok"] is True


def output_frames(server: FakeGateway, terminal_id: str) -> list[dict[str, Any]]:
    return [
        frame for frame in server.frames("terminal.output") if frame["terminal_id"] == terminal_id
    ]


def output_bytes(server: FakeGateway, terminal_id: str) -> bytes:
    return b"".join(base64.b64decode(frame["data"]) for frame in output_frames(server, terminal_id))


async def wait_for_output(
    server: FakeGateway, terminal_id: str, marker: bytes, timeout: float = 20.0
) -> None:
    """Wait until the shell has answered with `marker`; the bytes are never printed."""

    async def poll() -> None:
        while marker not in output_bytes(server, terminal_id):
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


async def wait_for_bytes(
    server: FakeGateway, terminal_id: str, total: int, timeout: float = 30.0
) -> None:
    async def poll() -> None:
        while len(output_bytes(server, terminal_id)) < total:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


async def wait_for_exit(
    server: FakeGateway, terminal_id: str, timeout: float = 20.0
) -> dict[str, Any]:
    async def poll() -> dict[str, Any]:
        while True:
            for frame in server.frames("terminal.exited"):
                if frame["terminal_id"] == terminal_id:
                    return frame
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout=timeout)


async def close_terminal(
    server: FakeGateway, terminal_id: str, *, holder: str = HOLDER
) -> dict[str, Any]:
    reply = await ask(
        server, {"type": "terminal.close", "from": holder, "terminal_id": terminal_id}
    )
    assert reply["ok"] is True
    return await wait_for_exit(server, terminal_id)


# ------------------------------------------------------------------ the shell


async def test_hello_says_this_device_offers_a_terminal(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    hello = server.frames("hello")[0]
    assert hello["terminal"] is True
    validate(hello, "Hello")


async def test_a_terminal_streams_the_shell_to_the_connection_that_opened_it(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    terminal_id = await open_terminal(server)
    assert daemon.terminals.count == 1
    # The quotes keep the terminal's echo of the command from carrying the
    # marker, so only the shell's own answer can satisfy the wait.
    await type_line(server, terminal_id, "echo re''mote")
    await wait_for_output(server, terminal_id, b"remote")
    frames = output_frames(server, terminal_id)
    assert [frame["seq"] for frame in frames] == list(range(1, len(frames) + 1))
    assert {frame["to"] for frame in frames} == {HOLDER}
    for frame in frames:
        validate(frame, "TerminalOutput")
    exited = await close_terminal(server, terminal_id)
    validate(exited, "TerminalExited")
    assert daemon.terminals.count == 0


async def test_resize_reaches_the_shell(running_daemon: tuple[Daemon, FakeGateway]) -> None:
    _, server = running_daemon
    terminal_id = await open_terminal(server, cols=80, rows=24)
    reply = await ask(
        server,
        {
            "type": "terminal.resize",
            "from": HOLDER,
            "terminal_id": terminal_id,
            "cols": 100,
            "rows": 32,
        },
    )
    assert reply["ok"] is True
    await type_line(server, terminal_id, "stty size")
    await wait_for_output(server, terminal_id, b"32 100")
    await close_terminal(server, terminal_id)


async def test_closing_a_terminal_ends_the_shell_and_reports_the_exit(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    terminal_id = await open_terminal(server)
    exited = await close_terminal(server, terminal_id)
    assert exited["to"] == HOLDER
    assert "code" in exited
    assert daemon.terminals.count == 0


async def test_a_shell_that_exits_by_itself_is_reported(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    terminal_id = await open_terminal(server)
    await type_line(server, terminal_id, "exit 3")
    exited = await wait_for_exit(server, terminal_id)
    assert exited["code"] == 3
    assert daemon.terminals.count == 0


async def test_a_long_burst_is_cut_into_frames_with_a_rising_seq(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    terminal_id = await open_terminal(server, cols=500, rows=200)
    await type_line(server, terminal_id, "printf '%040000d\\n' 0")
    await wait_for_bytes(server, terminal_id, 40000)
    frames = output_frames(server, terminal_id)
    assert len(frames) >= 3
    assert max(len(base64.b64decode(frame["data"])) for frame in frames) <= MAX_OUTPUT_BYTES
    assert [frame["seq"] for frame in frames] == list(range(1, len(frames) + 1))
    await close_terminal(server, terminal_id)


async def test_every_terminal_ends_when_the_daemon_stops(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    terminal_id = await open_terminal(server)
    await daemon.terminals.stop()
    assert daemon.terminals.count == 0
    await wait_for_exit(server, terminal_id)


# ------------------------------------------------------- detaching, attaching


async def test_a_detached_terminal_stops_streaming_but_keeps_the_shell(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    daemon.terminals.keep_alive = 30.0
    terminal_id = await open_terminal(server)
    await type_line(server, terminal_id, "echo be''fore")
    await wait_for_output(server, terminal_id, b"before")
    await detach(server, terminal_id)
    seen = len(output_frames(server, terminal_id))
    await type_line(server, terminal_id, "echo af''ter")
    await asyncio.sleep(0.4)
    assert len(output_frames(server, terminal_id)) == seen
    assert daemon.terminals.count == 1
    await close_terminal(server, terminal_id)


async def test_a_detached_terminal_ends_when_its_keep_alive_runs_out(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    daemon.terminals.keep_alive = 0.2
    terminal_id = await open_terminal(server)
    await detach(server, terminal_id)
    exited = await wait_for_exit(server, terminal_id)
    assert exited["to"] == HOLDER
    assert daemon.terminals.count == 0


async def test_attach_hands_back_the_scrollback_and_moves_the_output(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    daemon, server = running_daemon
    daemon.terminals.keep_alive = 30.0
    terminal_id = await open_terminal(server, cols=90, rows=30)
    await type_line(server, terminal_id, "echo ma''rker")
    await wait_for_output(server, terminal_id, b"marker")
    before = output_frames(server, terminal_id)[-1]["seq"]
    await detach(server, terminal_id)
    reply = await ask(
        server, {"type": "terminal.attach", "from": "app-2", "terminal_id": terminal_id}
    )
    assert reply["ok"] is True
    result = reply["result"]
    assert result["terminal_id"] == terminal_id
    assert (result["cols"], result["rows"]) == (90, 30)
    assert b"marker" in base64.b64decode(result["scrollback"])
    await type_line(server, terminal_id, "echo se''cond", holder="app-2")
    await wait_for_output(server, terminal_id, b"second")
    moved = [frame for frame in output_frames(server, terminal_id) if frame["seq"] > before]
    assert moved
    assert {frame["to"] for frame in moved} == {"app-2"}
    await close_terminal(server, terminal_id, holder="app-2")


# ------------------------------------------------------------------- refusals


async def test_a_fifth_terminal_is_refused(running_daemon: tuple[Daemon, FakeGateway]) -> None:
    daemon, server = running_daemon
    opened = [await open_terminal(server) for _ in range(MAX_TERMINALS)]
    assert daemon.terminals.count == MAX_TERMINALS
    reply = await ask(server, {"type": "terminal.open", "from": HOLDER, "cols": 80, "rows": 24})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "conflict"
    for terminal_id in opened:
        await close_terminal(server, terminal_id)
    assert daemon.terminals.count == 0


async def test_input_larger_than_the_limit_is_refused(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    terminal_id = await open_terminal(server)
    oversized = base64.b64encode(b"x" * (MAX_INPUT_BYTES + 1)).decode("ascii")
    reply = await ask(
        server,
        {
            "type": "terminal.input",
            "from": HOLDER,
            "terminal_id": terminal_id,
            "data": oversized,
        },
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"
    await close_terminal(server, terminal_id)


async def test_an_unknown_terminal_is_not_found(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await ask(
        server, {"type": "terminal.input", "from": HOLDER, "terminal_id": rid(), "data": "aGk="}
    )
    assert reply["ok"] is False
    assert reply["error"]["code"] == "not_found"


async def test_closing_an_unknown_terminal_is_no_error(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await ask(server, {"type": "terminal.close", "from": HOLDER, "terminal_id": rid()})
    assert reply["ok"] is True


async def test_a_size_outside_the_bounds_is_refused(
    running_daemon: tuple[Daemon, FakeGateway],
) -> None:
    _, server = running_daemon
    reply = await ask(server, {"type": "terminal.open", "from": HOLDER, "cols": 501, "rows": 24})
    assert reply["ok"] is False
    assert reply["error"]["code"] == "bad_request"


# ------------------------------------------------------- the switch in config


async def test_a_device_with_the_terminal_switched_off_answers_unsupported() -> None:
    async def publish(frame: dict[str, Any]) -> None:
        raise AssertionError("a disabled manager publishes nothing")

    manager = TerminalManager(publish, enabled=False)
    with pytest.raises(RcError) as opened:
        await manager.open({"from": HOLDER, "cols": 80, "rows": 24})
    assert opened.value.code == "unsupported"
    with pytest.raises(RcError) as typed:
        await manager.input({"from": HOLDER, "terminal_id": rid(), "data": "aGk="})
    assert typed.value.code == "unsupported"
    with pytest.raises(RcError) as closed:
        await manager.close({"from": HOLDER, "terminal_id": rid()})
    assert closed.value.code == "unsupported"
    assert manager.count == 0


def test_the_terminal_switch_defaults_to_on_and_round_trips() -> None:
    assert sample_config().terminal.enabled is True
    path = save_config(sample_config())
    assert load_config().terminal.enabled is True
    path.write_text(path.read_text().replace("enabled = true", "enabled = false"))
    assert load_config().terminal.enabled is False


async def test_hello_reports_a_terminal_that_is_switched_off() -> None:
    config = sample_config()
    config.terminal.enabled = False
    daemon = Daemon(config)
    try:
        hello = await daemon._hello()
    finally:
        daemon.registry.close()
    assert hello["terminal"] is False
