"""Shared helpers: a hub with two accounts' devices and sessions, and a tool runner on top."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from rc_gateway.live.access import AccountAccess
from rc_gateway.live.drafts import DraftBox
from rc_gateway.live.gate import ConfirmGate
from rc_gateway.live.tools import ToolRunner
from rc_gateway.state import GatewayState

from ..conftest import HubRig, frames_of, hub_rig, session_summary

MINE = "aaaaaaaa-0000-4000-8000-000000000001"
SECOND = "aaaaaaaa-0000-4000-8000-000000000002"
THEIRS = "bbbbbbbb-0000-4000-8000-000000000001"


@dataclass
class LiveRig:
    hub: HubRig
    access: AccountAccess
    drafts: DraftBox
    tools: ToolRunner
    gate: ConfirmGate
    #: Frames to the computer that this rig has taken off its queue, so waiting for one is not
    #: taking it from whoever answers it.
    at_device: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(await self.tools.run(name, json.dumps(arguments)))
        return result

    async def publish(self, session_id: str, **fields: Any) -> None:
        summary = session_summary(session_id, self.hub.device_id, **fields)
        await self.hub.hub.handle_device_frame(
            self.hub.device, {"type": "session.updated", "session": summary}
        )

    async def event(self, session_id: str, event: dict[str, Any]) -> None:
        await self.hub.hub.handle_device_frame(
            self.hub.device,
            {"type": "session.event", "session_id": session_id, "event": event},
        )

    def _requests(self) -> list[dict[str, Any]]:
        """The sends the computer has been forwarded and has not answered."""
        self.at_device.extend(frames_of(self.hub.device))
        return [frame for frame in self.at_device if frame.get("type") == "session.send"]

    async def send_reached_device(self) -> None:
        """Wait until the forwarded ``session.send`` is at the computer, and leave it unanswered."""
        for _ in range(200):
            if self._requests():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no session.send reached the device")

    async def answer_device(self, accepted: str = "sent") -> dict[str, Any]:
        """Play the computer: take the forwarded request and reply to it."""
        return await self._reply_to_send({"ok": True, "result": {"accepted": accepted}})

    async def refuse_device(self, code: str = "device_busy") -> dict[str, Any]:
        """Play the computer: take the forwarded request and refuse it."""
        return await self._reply_to_send({"ok": False, "error": {"code": code, "message": "no"}})

    async def _reply_to_send(self, outcome: dict[str, Any]) -> dict[str, Any]:
        await self.send_reached_device()
        request = self._requests()[0]
        self.at_device.remove(request)
        reply = {"type": "reply", "id": request["id"], "from": request["from"], **outcome}
        await self.hub.hub.handle_device_frame(self.hub.device, reply)
        return request


async def live_rig(tmp_path: Path) -> LiveRig:
    rig = await hub_rig(tmp_path)
    other = await rig.devices.create_pairing("mallory")
    enrolled = await rig.devices.redeem(
        other.code,
        name="their-mac",
        platform="macos",
        hostname="theirs.local",
        arch="arm64",
        client_version="0.1.0",
    )
    assert not isinstance(enrolled, str)
    await rig.index.upsert(
        session_summary(
            THEIRS, enrolled.device_id, title="Their secret project", cwd="/home/them/secret"
        )
    )
    state = cast(
        GatewayState,
        SimpleNamespace(devices=rig.devices, index=rig.index, hub=rig.hub),
    )
    drafts, gate = DraftBox(), ConfirmGate()
    access = AccountAccess(state, "admin")
    tools = ToolRunner(access, drafts, gate)
    return LiveRig(rig, access, drafts, tools, gate)
