"""A60 and its log line: a `session.history` an app sends reaches the device as it was sent.

`brief` is the device's to read, so the gateway forwards it untouched like every other field it
does not route by, and says how long each page took and how big it was, never what was in it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from .catch_up_support import (
    SESSION_ID,
    Watching,
    block,
    history_reply,
    history_request,
    put_session,
)
from .conftest import hub_rig


@pytest.mark.asyncio
async def test_a_brief_history_request_is_forwarded_untouched_and_logged_by_its_numbers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await hub_rig(tmp_path)
    watching = Watching(rig.app)
    await put_session(rig, last_seq=12)
    asked = {
        "type": "session.history",
        "id": "h1",
        "session_id": SESSION_ID,
        "limit": 200,
        "brief": True,
    }
    with caplog.at_level(logging.INFO, logger="rc_gateway.hub"):
        await rig.hub.handle_app_frame(rig.app, asked)
        request = await history_request(rig.device)
        assert request is not None
        assert {key: request[key] for key in asked} == asked
        reply = history_reply(request, [block(seq, "secret words") for seq in range(1, 13)])
        size = len(json.dumps(reply, ensure_ascii=False))
        await rig.hub.handle_device_frame(rig.device, reply, size=size)
        answered = await watching.wait_reply("h1")

    assert len(answered["result"]["events"]) == 12
    (record,) = [item for item in caplog.records if item.getMessage() == "history forwarded"]
    fields = record.fields  # type: ignore[attr-defined]
    assert fields["device_id"] == rig.device_id
    assert fields["session_id"] == SESSION_ID
    assert (fields["brief"], fields["ok"], fields["events"], fields["chars"]) == (
        True,
        True,
        12,
        size,
    )
    assert isinstance(fields["ms"], int)
    assert "secret words" not in str(fields)
    await rig.hub.stop()


@pytest.mark.asyncio
async def test_a_refused_history_request_is_logged_too(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = await hub_rig(tmp_path)
    await put_session(rig, last_seq=3)
    with caplog.at_level(logging.INFO, logger="rc_gateway.hub"):
        await rig.hub.handle_app_frame(
            rig.app, {"type": "session.history", "id": "h2", "session_id": SESSION_ID}
        )
        request = await history_request(rig.device)
        assert request is not None
        refused = {
            "type": "reply",
            "id": request["id"],
            "from": request["from"],
            "ok": False,
            "error": {"code": "bad_request", "message": "brief must be a boolean"},
        }
        await rig.hub.handle_device_frame(rig.device, refused)

    (record,) = [item for item in caplog.records if item.getMessage() == "history forwarded"]
    fields = record.fields  # type: ignore[attr-defined]
    assert (fields["brief"], fields["ok"], fields["events"]) == (False, False, None)
    await rig.hub.stop()
