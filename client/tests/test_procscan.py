"""Reading `lsof -F` reports, and failing closed when one cannot be read."""

from __future__ import annotations

import pytest

from rc_client import procscan

# A real `lsof -n -P -w -p … -F pftn` report from a Mac, trimmed: a Codex TUI
# with its working directory, a mapped executable, its terminal and a rollout it
# writes itself, then the shared daemon with nothing but a working directory.
CAPTURED = """p97125
fcwd
tDIR
n/Users/me/dev/remote-control
ftxt
tREG
n/Users/me/.codex/packages/standalone/releases/0.154.0/bin/codex
f0
tCHR
n/dev/ttys002
f74
tREG
n/Users/me/.codex/sessions/2026/09/12/rollout-2026-09-12T03-27-20.jsonl
p92162
fcwd
tDIR
n/
"""


async def test_open_paths_reads_a_captured_lsof_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(procscan, "IS_LINUX", False)

    async def fake_run(*args: str, timeout: float = procscan.SCAN_TIMEOUT) -> tuple[int, str]:
        return 0, CAPTURED

    monkeypatch.setattr(procscan, "_run", fake_run)
    found, complete = await procscan.process_open_paths([97125, 92162])

    assert complete is True
    tui = found[97125]
    assert tui.cwd == "/Users/me/dev/remote-control"
    # The terminal is not a file, and the working directory is not one either.
    assert tui.files == (
        "/Users/me/.codex/packages/standalone/releases/0.154.0/bin/codex",
        "/Users/me/.codex/sessions/2026/09/12/rollout-2026-09-12T03-27-20.jsonl",
    )
    assert found[92162] == procscan.OpenPaths(cwd="/", files=())


async def test_open_paths_fails_closed_when_lsof_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(procscan, "IS_LINUX", False)

    async def timed_out(*args: str, timeout: float = procscan.SCAN_TIMEOUT) -> tuple[int, str]:
        return 124, ""

    monkeypatch.setattr(procscan, "_run", timed_out)
    assert await procscan.process_open_paths([97125]) == ({}, False)
    # Nothing to ask about is a complete answer, not an unknown one.
    assert await procscan.process_open_paths([]) == ({}, True)
