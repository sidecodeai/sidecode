"""What the browser tells a call about itself: the session page on screen, and its player."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.live.focus import Focus

from .fakes import QUIET, FakeClock, until
from .harness import Harness, function_call, start, usage_records
from .rig import MINE, SECOND

BAD_FOCUS: list[dict[str, Any]] = [
    {"device_id": 7, "session_id": "s"},
    {"device_id": "d"},
    {"device_id": "d", "session_id": ["s"]},
    {"device_id": "d", "session_id": "s" * 201},
    {"device_id": "", "session_id": ""},
    {"device_id": None, "session_id": None},
    {},
]
BAD_PLAYBACK: list[dict[str, Any]] = [
    {"underruns": -1, "gap_ms": 5, "restarts": 0, "delay_ms": -400},
    {"underruns": 1.5, "gap_ms": 5, "restarts": 0, "delay_ms": -400},
    {"underruns": "1", "gap_ms": 5, "restarts": 0, "delay_ms": "400"},
    {"underruns": True, "gap_ms": 5, "restarts": 0, "delay_ms": True},
    {"underruns": 1, "gap_ms": 5, "delay_ms": 0.4},
    {"underruns": None, "gap_ms": None, "restarts": None, "delay_ms": None},
    {},
]


def focus_of(h: Harness) -> Focus | None:
    """What the call last took the browser to have on screen (read fresh each time)."""
    return h.rig.tools.focus


async def settle_on_a_frame(h: Harness) -> None:
    """Have the call take a microphone frame after the commands before it: they are done by then."""
    sent = len(h.upstream.of("session.input_audio.append"))
    h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == sent + 1)


async def test_the_focus_marks_that_session_in_the_list_the_backend_reads(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="running")
    await h.rig.publish(SECOND, title="Migrate billing", state="idle")
    h.browser.command(type="focus", device_id=h.rig.hub.device_id, session_id=SECOND)
    await settle_on_a_frame(h)
    h.upstream.push(function_call("list_sessions", {}))
    await until(lambda: "response.create" in h.upstream.types())
    output = json.loads(h.upstream.of("response.item.create")[0]["item"]["output"])
    assert {row["id"]: row.get("on_screen") for row in output["sessions"]} == {
        MINE: None,
        SECOND: True,
    }
    await h.stop()


async def test_a_newer_focus_replaces_the_older_and_a_null_one_clears_it(tmp_path: Path) -> None:
    h = await start(tmp_path)
    device = h.rig.hub.device_id
    h.browser.command(type="focus", device_id=device, session_id=MINE)
    await settle_on_a_frame(h)
    assert focus_of(h) == Focus(device, MINE)
    h.browser.command(type="focus", device_id=device, session_id=SECOND)
    await settle_on_a_frame(h)
    assert focus_of(h) == Focus(device, SECOND)
    h.browser.command(type="focus", device_id=None, session_id=None)
    await settle_on_a_frame(h)
    assert focus_of(h) is None
    await h.stop()


@pytest.mark.parametrize("bad", BAD_FOCUS)
async def test_a_bad_focus_means_no_focus_and_never_ends_the_call(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    h = await start(tmp_path)
    h.browser.command(type="focus", device_id="d", session_id="s")
    await settle_on_a_frame(h)
    assert focus_of(h) == Focus("d", "s")
    h.browser.command(type="focus", **bad)
    await settle_on_a_frame(h)
    assert focus_of(h) is None
    assert h.call.active
    await h.stop()


async def test_the_latest_playback_report_is_in_the_usage_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.browser.command(type="playback", underruns=1, gap_ms=120, restarts=0)
    h.browser.command(type="playback", underruns=4, gap_ms=950, restarts=2)
    # The last report comes just before the hang-up, and still counts.
    h.browser.command(type="playback", underruns=5, gap_ms=1100, restarts=2)
    h.browser.command(type="hangup")
    await h.finished()
    [record] = usage_records(caplog)
    assert (record["browser_underruns"], record["browser_gap_ms"]) == (5, 1100)
    assert record["browser_restarts"] == 2


async def test_a_browser_that_never_reports_is_null_in_the_usage_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await h.stop()
    [record] = usage_records(caplog)
    assert [record[f"browser_{name}"] for name in ("underruns", "gap_ms", "restarts")] == [None] * 3


@pytest.mark.parametrize("bad", BAD_PLAYBACK)
async def test_a_bad_playback_report_is_ignored_and_never_ends_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad: dict[str, Any]
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.browser.command(type="playback", underruns=2, gap_ms=30, restarts=1, delay_ms=500)
    h.browser.command(type="playback", **bad)
    await settle_on_a_frame(h)
    assert h.call.active
    await h.stop()
    [record] = usage_records(caplog)
    assert (record["browser_underruns"], record["browser_gap_ms"]) == (2, 30)
    assert record["browser_delay_ms"] == 500


async def test_a_report_beyond_anything_real_is_clamped_in_the_usage_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.browser.command(type="playback", underruns=10**30, gap_ms=10**12, restarts=7)
    await settle_on_a_frame(h)
    await h.stop()
    [record] = usage_records(caplog)
    assert record["browser_underruns"] == 1_000_000_000
    assert (record["browser_gap_ms"], record["browser_restarts"]) == (1_000_000_000, 7)


@pytest.mark.parametrize(
    "command",
    [
        {"type": "focus", "device_id": "d", "session_id": "s"},
        {"type": "playback", "underruns": 1, "gap_ms": 2, "restarts": 3},
    ],
    ids=["focus", "playback"],
)
async def test_both_go_through_the_command_gate_like_every_other_command(
    tmp_path: Path, command: dict[str, Any]
) -> None:
    h = await start(tmp_path, clock=FakeClock())
    for _ in range(40):
        h.browser.command(**command)
    await h.finished()
    assert h.browser.ended == "protocol_error"


async def test_a_few_of_them_in_a_burst_are_a_person_navigating(tmp_path: Path) -> None:
    h = await start(tmp_path, clock=FakeClock())
    for _ in range(8):
        h.browser.command(type="focus", device_id="d", session_id="s")
        h.browser.command(type="playback", underruns=1, gap_ms=2, restarts=3)
    await settle_on_a_frame(h)
    assert h.call.active
    await h.stop()
