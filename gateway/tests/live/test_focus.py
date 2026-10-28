"""The session page the browser has open: validated, and only a mark on a row already listed."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rc_gateway.live.focus import MAX_ID_CHARS, Focus, parse_focus

from .rig import MINE, SECOND, THEIRS, live_rig

LONGEST = "x" * MAX_ID_CHARS


@pytest.fixture(autouse=True)
def frozen_ages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row says how long ago its session was active: hold the clock so answers can be equal."""
    monkeypatch.setattr(
        "rc_gateway.live.digest.time", SimpleNamespace(time=lambda: 1_800_000_000.0)
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ({"device_id": "d1", "session_id": "s1"}, Focus("d1", "s1")),
        ({"device_id": LONGEST, "session_id": LONGEST}, Focus(LONGEST, LONGEST)),
        ({"device_id": None, "session_id": None}, None),
        ({"device_id": "d1", "session_id": None}, None),
        ({"device_id": None, "session_id": "s1"}, None),
        ({"device_id": "d1"}, None),
        ({}, None),
        ({"device_id": "", "session_id": "s1"}, None),
        ({"device_id": "d1", "session_id": ""}, None),
        ({"device_id": LONGEST + "x", "session_id": "s1"}, None),
        ({"device_id": "d1", "session_id": LONGEST + "x"}, None),
        ({"device_id": 7, "session_id": "s1"}, None),
        ({"device_id": "d1", "session_id": ["s1"]}, None),
        ({"device_id": True, "session_id": True}, None),
        ({"device_id": {"id": "d1"}, "session_id": "s1"}, None),
    ],
)
def test_a_focus_names_both_ids_as_short_strings_or_it_is_none(
    command: dict[str, Any], expected: Focus | None
) -> None:
    assert parse_focus(command) == expected


async def test_list_sessions_marks_the_session_page_that_is_on_screen(tmp_path: Path) -> None:
    rig = await live_rig(tmp_path)
    await rig.publish(MINE, title="Fix flaky auth test", state="running")
    await rig.publish(SECOND, title="Migrate billing", state="idle")
    rig.tools.focus = Focus(rig.hub.device_id, SECOND)
    listed = await rig.call("list_sessions")
    rows = {row["id"]: row for row in listed["sessions"]}
    assert rows[SECOND]["on_screen"] is True
    assert "on_screen" not in rows[MINE]


async def test_with_no_focus_no_row_is_marked(tmp_path: Path) -> None:
    rig = await live_rig(tmp_path)
    await rig.publish(MINE, title="Fix flaky auth test")
    assert "on_screen" not in json.dumps(await rig.call("list_sessions"))


async def test_a_focus_on_a_session_the_account_does_not_list_changes_nothing(
    tmp_path: Path,
) -> None:
    rig = await live_rig(tmp_path)
    await rig.publish(MINE, title="Fix flaky auth test", state="running")
    before = await rig.call("list_sessions")
    theirs = (await rig.hub.devices.list_for_user("mallory"))[0].device_id
    for focus in (Focus(theirs, THEIRS), Focus(rig.hub.device_id, THEIRS), Focus("none", "none")):
        rig.tools.focus = focus
        assert await rig.call("list_sessions") == before
    assert "on_screen" not in json.dumps(before)
    assert THEIRS not in json.dumps(before)


async def test_a_session_id_on_the_wrong_device_is_not_the_one_on_screen(tmp_path: Path) -> None:
    rig = await live_rig(tmp_path)
    await rig.publish(MINE, title="Fix flaky auth test")
    rig.tools.focus = Focus("another-device", MINE)
    assert "on_screen" not in json.dumps(await rig.call("list_sessions"))


async def test_the_focus_is_never_an_access_decision(tmp_path: Path) -> None:
    rig = await live_rig(tmp_path)
    await rig.publish(MINE, title="Fix flaky auth test")
    theirs = (await rig.hub.devices.list_for_user("mallory"))[0].device_id
    rig.tools.focus = Focus(theirs, THEIRS)
    assert (await rig.call("get_session_status", session=THEIRS))["error"] == "unknown_session"
    assert (await rig.call("get_session_context", session=THEIRS))["error"] == "unknown_session"
    assert (await rig.call("prepare_message", session=THEIRS, text="hi"))[
        "error"
    ] == "unknown_session"


async def test_the_focus_changes_no_other_tools_answer(tmp_path: Path) -> None:
    rig = await live_rig(tmp_path)
    await rig.publish(MINE, title="Fix flaky auth test", state="running")
    status = await rig.call("get_session_status", session=MINE)
    pending = await rig.call("list_pending_decisions")
    context = await rig.call("get_session_context", session=MINE)
    rig.tools.focus = Focus(rig.hub.device_id, MINE)
    assert await rig.call("get_session_status", session=MINE) == status
    assert await rig.call("list_pending_decisions") == pending
    assert await rig.call("get_session_context", session=MINE) == context


async def test_the_session_on_screen_is_listed_first_so_the_cap_on_the_list_cannot_cut_it(
    tmp_path: Path,
) -> None:
    rig = await live_rig(tmp_path)
    for index in range(35):
        await rig.publish(f"dddddddd-0000-4000-8000-{index:012d}", title=f"Session {index}")
    everything = [str(item["session_id"]) for item in await rig.access.sessions()]
    beyond = everything[33]  # the 34th the account lists: past the cap of 30
    unfocused = await rig.call("list_sessions")
    unfocused_ids = [row["id"] for row in unfocused["sessions"]]
    assert unfocused_ids == everything[:30]

    rig.tools.focus = Focus(rig.hub.device_id, beyond)
    focused = await rig.call("list_sessions")
    rows = focused["sessions"]
    assert (len(rows), focused["total"]) == (30, 35)
    assert (rows[0]["id"], rows[0]["on_screen"]) == (beyond, True)
    assert [row["id"] for row in rows[1:]] == everything[:29]
    assert sum("on_screen" in row for row in rows) == 1
