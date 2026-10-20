"""What a late ending does to a session whose turn is already over (A52).

An answer that lands after the turn ended — the leader's own, or the tool's
result — settles its card and nothing more: it must never put a finished
session back to `running`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from rc_client.agents.grok.dialogs import DialogDesk
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.test_grok_runner import Recorder, wait_for

ENDINGS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "grok" / "question-endings.json").read_text(
        encoding="utf-8"
    )
)


def desk(tmp_path: Path) -> tuple[DialogDesk, SessionChannel, Recorder]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="grok", cwd=str(tmp_path))
    registry.upsert_session(session)
    recorder = Recorder()
    channel = SessionChannel(registry, session, recorder)
    return DialogDesk(channel, shared=lambda: True), channel, recorder


async def test_a_question_settled_after_the_turn_ended_leaves_the_session_idle(
    tmp_path: Path,
) -> None:
    dialogs, channel, recorder = desk(tmp_path)
    ending = ENDINGS["answered"]
    asking = asyncio.create_task(dialogs.request("_x.ai/ask_user_question", ending["request"]))
    await wait_for(lambda: channel.session.state == "needs_input")

    await channel.set_state("idle")
    await dialogs.tool_result(ending["result"])
    assert await asking is None
    assert recorder.events("question")[-1]["by"] == "terminal"
    assert channel.session.state == "idle"
    channel.registry.close()


async def test_an_approval_settled_after_the_turn_ended_leaves_the_session_idle(
    tmp_path: Path,
) -> None:
    dialogs, channel, recorder = desk(tmp_path)
    request = {
        "sessionId": "s1",
        "toolCall": {"toolCallId": "call-1", "title": "write", "rawInput": {"path": "x.txt"}},
        "options": [{"optionId": "allow-once", "name": "Yes", "kind": "allow_once"}],
    }
    asking = asyncio.create_task(dialogs.request("session/request_permission", request))
    await wait_for(lambda: channel.session.state == "needs_approval")

    await channel.set_state("idle")
    await dialogs.resolved_elsewhere("call-1")
    await asking
    assert recorder.events("approval")[-1]["decision"] == {
        "option_id": "elsewhere",
        "by": "terminal",
    }
    assert channel.session.state == "idle"
    channel.registry.close()
