"""A long pi session no longer breaks its extension's link.

The `hello` used to carry every entry of the branch, and the socket read lines
of at most 64 KiB, so a long session's `hello` failed, the extension dialled
again a second later, and failed again. The line limit is now a frame's, a line
longer than that is skipped rather than ending the connection, and the
extension's `hello` names the session file, which the device reads the branch
from when it first meets the session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rc_client.agents.pi import frames, session_file
from rc_client.agents.pi import runtime as pi_runtime
from tests.test_pi_extension import FIXTURES, SESSION, Bench, bench, connect, settle

__all__ = ["bench"]


def write_session(directory: Path, entries: list[dict[str, object]]) -> Path:
    """A pi session file as pi keeps it: a header, then one entry per line."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2026-11-16T08-00-00-000Z_{SESSION}.jsonl"
    header = {"type": "session", "version": 3, "id": SESSION, "cwd": "/repo"}
    rows = [header, *entries]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.fixture
def sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pi-sessions"
    monkeypatch.setattr(pi_runtime, "sessions_dir", lambda: root)
    return root


async def test_the_branch_is_read_from_the_session_file(bench: Bench, sessions: Path) -> None:
    branch = json.loads((FIXTURES / "branch.json").read_text(encoding="utf-8"))
    # A side branch the session left: in the file, but not on the path it is on.
    side = {**branch[0], "id": "side0001", "parentId": None}
    path = write_session(sessions / "--repo--", [side, *branch])
    extension = await connect(bench)
    await extension.hello(entries=None, session_file=str(path), first_prompt="Run the bash command")
    await settle()
    assert [event["text"] for event in bench.events("user_message")] == [
        "Run the bash command: echo hello-pi. Then say done."
    ]
    assert bench.entry().session.title.startswith("Run the bash command")
    extension.close()


def test_a_file_that_is_not_the_session_s_own_gives_no_branch(
    sessions: Path, tmp_path: Path
) -> None:
    inside = write_session(
        sessions / "--repo--", [{"type": "message", "id": "a", "parentId": None}]
    )
    assert session_file.branch(SESSION, str(inside)) != []
    assert session_file.branch("another-session", str(inside)) == []
    outside = write_session(tmp_path / "elsewhere", [{"type": "message", "id": "a"}])
    assert session_file.branch(SESSION, str(outside)) == []
    assert session_file.branch(SESSION, None) == []


async def test_a_hello_past_the_old_line_limit_registers(bench: Bench) -> None:
    long_name = "x" * (200 * 1024)
    extension = await connect(bench)
    welcome = await extension.hello(name=long_name[:50], padding=long_name)
    assert welcome["type"] == frames.WELCOME
    assert SESSION in bench.hub.entries
    extension.close()


async def test_a_line_past_the_limit_is_skipped_and_the_link_lives_on(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    await bench.server.stop()
    monkeypatch.setattr(frames, "MAX_FRAME_BYTES", 256 * 1024)
    await bench.server.start()
    extension = await connect(bench)
    await extension.hello()
    # Past the frame limit: dropped, not the end of the connection.
    extension.writer.write(b'{"type": "event", "event": {"pad": "' + b"y" * (1 << 20) + b'"}}\n')
    await extension.writer.drain()
    await extension.event({"type": "agent_start"})
    await settle()

    async def turned() -> None:
        while bench.entry().session.state != "running":
            await asyncio.sleep(0.02)

    await asyncio.wait_for(turned(), timeout=5)
    extension.close()
