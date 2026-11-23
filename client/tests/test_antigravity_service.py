"""Antigravity's conversations as the device's sessions, through the hub (A64, 4.4)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents import registry as agents
from rc_client.agents.antigravity import errors, plugin, runtime
from rc_client.agents.antigravity import service as service_module
from rc_client.agents.antigravity.service import AntigravityService
from rc_client.config import MirrorConfig
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub
from tests.agy_helpers import SESSION, agent_info, fake_agy, wait_for

OTHER = "7a1c9f3e-0b2d-4e5f-8a6b-1c2d3e4f5a6b"
OLD = "11111111-2222-4333-8444-555555555555"
# The service's own loop, kept before `by_hand` turns it off for every other test.
REAL_EVERY = AntigravityService._every


def iso(seconds_ago: float) -> str:
    moment = time.gmtime(time.time() - seconds_ago)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", moment)


class Built:
    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        limits: MirrorConfig | None = None,
        **config: Any,
    ) -> None:
        self.binary, self.peer = fake_agy(tmp_path, **config)
        self.limits = limits or MirrorConfig()
        # How the service finds `agy` for itself: the override points at the fake.
        monkeypatch.setenv("RC_ANTIGRAVITY_BIN", self.binary)
        self.registry = Registry(tmp_path / "state.sqlite3")
        self.frames: list[dict[str, Any]] = []

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", lambda: [agent_info(self.binary)])
        self.service = plugin.SERVICE
        self.refreshed = 0

    async def start(self) -> None:
        async def refresh() -> None:
            self.refreshed += 1

        await self.service.start(self.hub, refresh, self.limits)

    async def stop(self) -> None:
        await self.service.stop()
        await self.hub.close()
        self.registry.close()

    def removed(self) -> list[str]:
        return [f["session_id"] for f in self.frames if f.get("type") == "session.removed"]


@pytest.fixture
def lists_dir(tmp_path: Path) -> str:
    return str(runtime.lists_dir())


@pytest.fixture(autouse=True)
def by_hand(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service's own timers stay off: each test lists and scans when it says so."""

    async def never(self: AntigravityService, seconds: float, step: Any) -> None:
        return None

    monkeypatch.setattr(AntigravityService, "_every", never)


def conversations(lists_dir: str, work: Path) -> list[dict[str, Any]]:
    return [
        {"sessionId": SESSION, "cwd": str(work), "title": "Fix the build", "updatedAt": iso(60)},
        {"sessionId": OTHER, "cwd": lists_dir, "title": "No folder", "updatedAt": iso(120)},
        {"sessionId": OLD, "cwd": str(work), "title": "Long ago", "updatedAt": iso(30 * 86400)},
    ]


async def test_agy_s_conversations_are_published_as_it_lists_them(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path))
    await built.start()
    await built.service.list_once()
    try:
        fixed = built.hub.entry(SESSION).session
        assert (fixed.origin, fixed.control, fixed.state, fixed.title) == (
            "terminal",
            "none",
            "idle",
            "Fix the build",
        )
        assert fixed.cwd == str(tmp_path)
        assert abs(fixed.updated_at - (time.time() - 60) * 1000) < 2000, "agy's own last write"
        homeless = built.hub.entry(OTHER).session
        assert homeless.cwd == str(Path.home()), "a conversation without a folder shows at home"
        assert OLD not in built.hub.entries, "the newest of the last fortnight only"
        published = [
            f["session"]["session_id"] for f in built.frames if f.get("type") == "session.updated"
        ]
        assert {SESSION, OTHER} <= set(published)
    finally:
        await built.stop()


async def test_later_lists_move_the_title_and_the_time_but_not_as_activity(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path))
    await built.start()
    try:
        await built.service.list_once()
        changed = conversations(lists_dir, tmp_path)
        changed[0] = {**changed[0], "title": "Fix the build, again", "updatedAt": iso(5)}
        built.peer.configure(list=changed)
        await built.service.list_once()
        session = built.hub.entry(SESSION).session
        assert session.title == "Fix the build, again"
        assert abs(session.updated_at - (time.time() - 5) * 1000) < 2000
    finally:
        await built.stop()


async def test_a_conversation_gone_from_two_lists_is_withdrawn_unless_held(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path))
    await built.start()
    try:
        await built.service.list_once()
        built.hub.entry(OTHER).session.held = True
        built.peer.configure(list=[])
        await built.service.list_once()
        assert SESSION in built.hub.entries, "one list missing it is not enough"
        await built.service.list_once()
        assert SESSION not in built.hub.entries
        assert built.removed() == [SESSION]
        assert OTHER in built.hub.entries, "the device holds it"
    finally:
        await built.stop()


async def test_a_message_continues_a_listed_conversation_after_its_earlier_turns(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay = [
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "Earlier."}},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Done."}},
    ]
    built = Built(
        tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path), replay={SESSION: replay}
    )
    await built.start()
    try:
        await built.service.list_once()
        result = await built.hub.send({"id": "m-1", "session_id": SESSION, "text": "Go on."})
        assert result == {"accepted": "sent"}
        entry = built.hub.entry(SESSION)
        await wait_for(lambda: entry.session.turn is None and entry.session.state == "idle")
        texts = [
            f["event"].get("text")
            for f in built.frames
            if f.get("type") == "session.event"
            and f["event"]["kind"] in ("user_message", "assistant_text")
            and f["event"].get("text")
        ]
        assert texts[:3] == ["Earlier.", "Done.", "Go on."], "the earlier turns come first"
        assert (entry.session.control, entry.session.held) == ("remote", True)
        assert "session/load" in built.peer.methods()
    finally:
        await built.stop()


async def test_a_message_to_a_conversation_an_agy_may_have_open_is_refused(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path))
    agents.plugin("antigravity")
    await built.start()
    try:
        await built.service.list_once()
        entry = built.hub.entry(SESSION)
        await built.hub.terminal_holds(entry, True)
        for request in (
            built.hub.send({"id": "m-1", "session_id": SESSION, "text": "Hi."}),
            built.hub.command({"id": "c-1", "session_id": SESSION, "name": "compact"}),
        ):
            with pytest.raises(RcError) as caught:
                await request
            assert (caught.value.code, caught.value.message) == ("conflict", errors.HELD)
    finally:
        await built.stop()


async def test_nobody_signed_in_is_an_account_signed_in_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch, signed_out=True)
    await built.start()
    try:
        await built.service.list_once()
        assert built.service.signed_out
        from rc_client.agents.registry import DetectContext

        info = await plugin.detect(DetectContext(limits=True))
        assert info.accounts == []
        assert [f for f in built.peer.starts() if f["argv"][:1] == ["-p"]] == [], "no query run"
    finally:
        await built.stop()


async def test_a_new_session_s_options_become_what_detection_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch)
    await built.start()
    try:
        before = await plugin.detect(agents.DetectContext())
        assert before.models == [] and before.default_model is None
        created = await built.hub.create({"agent": "antigravity", "cwd": str(tmp_path)})
        assert created["session"]["session_id"] == SESSION
        after = await plugin.detect(agents.DetectContext())
        assert [choice.id for choice in after.models][:2] == ["gemini-4-neon", "gemini-4-carbon"]
        assert (after.default_model, after.default_effort, after.default_permission_mode) == (
            "gemini-4-neon",
            "high",
            "default",
        )
        assert built.refreshed == 1, "the agents are said again once"
    finally:
        await built.stop()


@pytest.mark.parametrize(
    ("limits", "kept"),
    [
        (MirrorConfig(max_sessions=1, max_age_days=14), {SESSION}),
        (MirrorConfig(max_sessions=50, max_age_days=1), {SESSION, OTHER}),
    ],
)
async def test_the_list_keeps_to_what_mirror_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limits: MirrorConfig, kept: set[str]
) -> None:
    """`[mirror]` bounds `agy`'s list as it bounds the device's other mirrors."""
    listed = [
        {"sessionId": SESSION, "cwd": str(tmp_path), "title": "Newest", "updatedAt": iso(60)},
        {"sessionId": OTHER, "cwd": str(tmp_path), "title": "Older", "updatedAt": iso(120)},
        {"sessionId": OLD, "cwd": str(tmp_path), "title": "Days ago", "updatedAt": iso(2 * 86400)},
    ]
    built = Built(tmp_path, monkeypatch, limits, list=listed)
    await built.start()
    try:
        await built.service.list_once()
        assert set(built.hub.entries) == kept
    finally:
        await built.stop()


async def test_a_list_the_link_is_down_for_asks_nothing_and_says_so(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path))
    await built.start()
    up = False
    built.hub.link_up = lambda: up
    try:
        assert await built.service.list_once() is False
        assert built.peer.calls("session/list") == [], "agy is not asked while the link is down"
        up = True
        assert await built.service.list_once() is True
        assert SESSION in built.hub.entries
    finally:
        await built.stop()


async def test_the_first_list_follows_the_link_at_once(
    tmp_path: Path, lists_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a whole `LIST_EVERY` later: a list the link was down for goes again soon after."""
    monkeypatch.setattr(service_module, "LINK_WAIT", 0.05)
    built = Built(tmp_path, monkeypatch, list=conversations(lists_dir, tmp_path))
    await built.start()
    up = False
    built.hub.link_up = lambda: up
    loop = asyncio.create_task(
        REAL_EVERY(built.service, service_module.LIST_EVERY, built.service.list_once)
    )
    try:
        await asyncio.sleep(0.2)
        assert SESSION not in built.hub.entries
        up = True
        await wait_for(lambda: SESSION in built.hub.entries, timeout=2)
    finally:
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop
        await built.stop()


def test_a_session_object_round_trips(tmp_path: Path) -> None:
    session = Session(session_id=SESSION, device_id="d", agent="antigravity", cwd=str(tmp_path))
    assert Session.from_dict(session.to_dict()).agent == "antigravity"
