"""Bypass permissions, the device's own Antigravity mode (A67).

`agy` offers a client no mode that runs without asking, so the device adds one after `agy`'s: the
session runs `agy` in `accept-edits`, and the device answers every permission request itself with
`agy`'s allow-once option, raising no card. Questions still go to the person; a card already open
when the session switched stays for the person; asking for another mode ends it at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import catalog, plugin, settings
from rc_client.agents.antigravity import runner as runner_module
from rc_client.agents.antigravity.dialogs import PERMISSION, Dialogs
from rc_client.agents.antigravity.opening import Opening
from rc_client.agents.antigravity.options import BYPASS, Running, offered_modes
from rc_client.agents.antigravity.presence import Holder, Holders
from rc_client.agents.antigravity.service import AntigravityService
from rc_client.agents.registry import DetectContext
from rc_client.models import Choice
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub
from tests.agy_helpers import (
    SESSION,
    Recorder,
    agent_info,
    entry_for,
    fake_agy,
    runner_for,
    wait_for,
)
from tests.test_antigravity_exemption import Bench

AGY_MODES = ["default", "accept-edits", "auto", "plan"]
ALLOW_ONCE = {"optionId": "allow", "name": "Allow", "kind": "allow_once"}
REJECT_ONCE = {"optionId": "deny", "name": "Deny", "kind": "reject_once"}
# A request as `agy` 1.5.16 sends it, under the session id it knows the session by.
LS = {
    "sessionId": "sid-1",
    "toolCall": {
        "toolCallId": "call_1",
        "name": "run_command",
        "kind": "execute",
        "status": "pending",
        "title": "ls",
        "rawInput": {"CommandLine": "ls"},
    },
    "options": [ALLOW_ONCE, REJECT_ONCE],
}


class Asked:
    """One session's dialogs alone, in Bypass permissions unless told otherwise."""

    def __init__(self, tmp_path: Path, *, bypassing: bool = True, session: str | None = "sid-1"):
        self.recorder = Recorder()
        self.answers: list[dict[str, Any]] = []

        async def respond(agy_id: Any, result: dict[str, Any]) -> None:
            self.answers.append(result)

        async def respond_error(agy_id: Any, code: int, message: str) -> None:
            self.answers.append({"error": code})

        entry = entry_for(tmp_path, self.recorder)
        self.dialogs = Dialogs(
            entry.channel,
            respond,
            respond_error,
            approves=lambda: bypassing,
            session_id=lambda: session,
        )

    async def ask(self, **changes: Any) -> None:
        params = {**LS, **changes}
        for key in [key for key, value in changes.items() if value is None]:
            params.pop(key)
        await self.dialogs.request("7", PERMISSION, params)

    def cards(self) -> list[dict[str, Any]]:
        return self.recorder.events("approval")


@pytest.fixture(autouse=True)
def quick_wakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_module, "WAKE_QUIET", 0.2)


def modes_set(peer: Any) -> list[str]:
    calls = peer.calls("session/set_config_option")
    return [call["value"] for call in calls if call["configId"] == "mode"]


def set_before_first_prompt(peer: Any) -> bool:
    """Whether the newest process had its mode set before the first prompt it was sent."""
    asked = [
        (message["method"], (message.get("params") or {}).get("configId"))
        for message in peer.requests()
        if message.get("method")
    ]
    opened = max(i for i, (method, _) in enumerate(asked) if method == "session/resume")
    prompt = next(i for i in range(opened, len(asked)) if asked[i][0] == "session/prompt")
    return ("session/set_config_option", "mode") in asked[opened:prompt]


def allowed(peer: Any) -> list[Any]:
    return [
        reply["result"]["outcome"].get("optionId")
        for reply in peer.replies()
        if "outcome" in (reply.get("result") or {})
    ]


async def in_bypass(tmp_path: Path, **config: Any) -> tuple[Any, Recorder, Any]:
    binary, peer = fake_agy(tmp_path, **config)
    recorder = Recorder()
    entry = entry_for(tmp_path, recorder, permission_mode=BYPASS.id)
    runner = runner_for(entry, binary)
    await runner.start()
    return runner, recorder, peer


def listed(tmp_path: Path) -> list[dict[str, Any]]:
    """`agy`'s list, naming the conversation so a new process can resume it."""
    return [
        {"sessionId": SESSION, "cwd": str(tmp_path), "title": "t", "updatedAt": "2026-11-25T01:00Z"}
    ]


def test_the_device_s_mode_comes_after_agy_s_own_and_takes_no_id_of_agy_s() -> None:
    offered = [Choice(BYPASS.id, "agy's own, one day"), Choice("default", "Default")]
    assert offered_modes(offered) == [Choice("default", "Default"), BYPASS]
    assert (BYPASS.id, BYPASS.label) == ("device-bypass-permissions", "Bypass permissions")


async def test_bypass_permissions_is_offered_but_never_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = fake_agy(tmp_path)
    monkeypatch.setenv("RC_ANTIGRAVITY_BIN", binary)
    before = await plugin.detect(DetectContext())
    assert [choice.id for choice in before.permission_modes] == [*AGY_MODES, BYPASS.id]
    assert before.default_permission_mode is None
    catalog.remember(
        Running(
            models=[Choice("gemini-4-neon", "Gemini 4 Neon")],
            model="gemini-4-neon",
            modes=[Choice(mode, mode) for mode in AGY_MODES],
            mode="default",
        )
    )
    after = await plugin.detect(DetectContext())
    assert [choice.id for choice in after.permission_modes] == [*AGY_MODES, BYPASS.id]
    assert after.default_permission_mode == "default", "agy's own, never the device's"


async def test_a_session_in_bypass_runs_accept_edits_and_asks_nobody(tmp_path: Path) -> None:
    runner, recorder, peer = await in_bypass(tmp_path, turns=["tool"])
    assert modes_set(peer) == ["accept-edits"]
    said = [
        event["permission_mode"] for event in recorder.events("meta") if "permission_mode" in event
    ]
    assert set(said) <= {BYPASS.id}, "never the mode agy runs it in"
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert recorder.events("approval") == [], "no card"
    assert "needs_approval" not in recorder.states()
    assert allowed(peer) == ["allow"]
    tool = [e for e in recorder.events("tool_call") if e["status"] == "succeeded"]
    assert tool and tool[-1]["title"] == "ls -la", "the tool shows as its row"
    assert runner.entry.session.permission_mode == BYPASS.id


async def test_questions_still_go_to_the_person(tmp_path: Path) -> None:
    runner, recorder, peer = await in_bypass(tmp_path, turns=["question"])
    await runner.send("Ask me.")
    await wait_for(lambda: recorder.events("question"))
    [pending] = recorder.events("question")
    assert pending["status"] == "pending" and peer.replies() == []
    assert await runner.answer(pending["request_id"], {"q1": ["2"]}) is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()
    assert recorder.events("question")[-1]["status"] == "resolved"


async def test_a_card_open_when_the_session_switches_stays_for_the_person(
    tmp_path: Path,
) -> None:
    binary, peer = fake_agy(tmp_path, turns=["tool", "tool"])
    recorder = Recorder()
    runner = runner_for(entry_for(tmp_path, recorder, permission_mode="default"), binary)
    await runner.start()
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("approval"))
    await runner.apply_settings(None, BYPASS.id, None)
    assert modes_set(peer)[-1] == "accept-edits"
    await wait_for(lambda: recorder.events("meta")[-1].get("permission_mode") == BYPASS.id)
    [pending] = recorder.events("approval")
    assert pending["status"] == "pending" and peer.replies() == [], "the open card is the person's"

    assert await runner.approve(pending["request_id"], "deny", None) is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.send("List them again.")
    await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
    await runner.close()
    assert allowed(peer) == ["deny", "allow"], "the person's answer, then the device's"
    assert len(recorder.events("approval")) == 2, "the first card, pending then resolved"


async def test_another_mode_ends_it_at_once_and_is_set_in_agy(tmp_path: Path) -> None:
    runner, recorder, peer = await in_bypass(tmp_path, turns=["tool"])
    await runner.apply_settings(None, "default", None)
    assert modes_set(peer) == ["accept-edits", "default"]
    await wait_for(lambda: recorder.events("meta")[-1].get("permission_mode") == "default")
    await runner.send("List the files.")
    await wait_for(lambda: recorder.events("approval"))
    assert recorder.events("approval")[0]["status"] == "pending", "the person is asked again"
    await runner.interrupt()
    await runner.close()


async def test_a_new_process_keeps_bypass_permissions(tmp_path: Path) -> None:
    binary, peer = fake_agy(tmp_path, turns=["ok", "tool"], list=listed(tmp_path))
    recorder = Recorder()
    entry = entry_for(tmp_path, recorder, permission_mode=BYPASS.id)
    first = runner_for(entry, binary)
    await first.start()
    await first.send("Hello.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await first.close()

    # The 30-minute release, a resume, a restart: the next process opens the conversation again.
    again = runner_for(entry, binary, opening=Opening("resume", SESSION))
    await again.start()
    await again.send("List the files.")
    await wait_for(lambda: len(recorder.events("turn_completed")) == 2)
    await again.close()

    assert modes_set(peer) == ["accept-edits", "accept-edits"]
    assert set_before_first_prompt(peer), "set again before the first prompt"
    assert recorder.events("approval") == [] and allowed(peer) == ["allow"]


async def test_a_session_can_be_created_in_it_through_the_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary, _ = fake_agy(tmp_path, turns=["tool"])
    monkeypatch.setenv("RC_ANTIGRAVITY_BIN", binary)
    registry = Registry(tmp_path / "hub.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(registry, publish, "dev-1", lambda: [agent_info(binary)])
    try:
        await hub.create(
            {
                "agent": "antigravity",
                "cwd": str(tmp_path),
                "permission_mode": BYPASS.id,
                "first_message": "List the files.",
            }
        )

        def kinds() -> list[str]:
            return [f["event"]["kind"] for f in frames if f.get("type") == "session.event"]

        await wait_for(lambda: "turn_completed" in kinds())
        assert "approval" not in kinds()
        assert hub.entry(SESSION).session.permission_mode == BYPASS.id
    finally:
        await hub.close()
        registry.close()


@pytest.mark.parametrize("named", ["another-session", None])
async def test_only_a_request_naming_the_session_is_approved(
    tmp_path: Path, named: str | None
) -> None:
    asked = Asked(tmp_path)
    await asked.ask(sessionId=named)
    assert asked.answers == [], "not approved"
    assert [card["status"] for card in asked.cards()] == ["pending"], "the person is asked"
    await asked.dialogs.let_go()


async def test_nothing_is_approved_before_agy_has_named_the_session(tmp_path: Path) -> None:
    asked = Asked(tmp_path, session=None)
    await asked.ask()
    assert asked.answers == [] and len(asked.cards()) == 1
    await asked.dialogs.let_go()


async def test_a_holder_the_device_warned_about_brings_the_cards_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4.4 rule 1: once the device said a terminal's `agy` may have the conversation open, the
    rest of its turn asks the person, Bypass permissions or not."""

    async def never(self: AntigravityService, seconds: float, step: Any) -> None:
        return None

    monkeypatch.setattr(AntigravityService, "_every", never)
    bench = Bench(tmp_path, monkeypatch, turns=["tool"], ask_after="warned")
    bench.peer.configure(list=bench.listed())
    await bench.start()
    try:
        await bench.hub.create(
            {
                "agent": "antigravity",
                "cwd": str(bench.work),
                "permission_mode": BYPASS.id,
                "first_message": "List the files.",
            }
        )
        entry = bench.entry()

        def events(kind: str) -> list[dict[str, Any]]:
            return [
                f["event"]
                for f in bench.frames
                if f.get("type") == "session.event" and f["event"]["kind"] == kind
            ]

        await wait_for(lambda: events("turn_started"))
        bench.holders = Holders([Holder(4242, SESSION, str(bench.work))])
        await bench.scan()
        assert len(events("notice")) == 1, "the device's warning"
        # Only now does `agy` ask: the turn ran, and the device warned, first.
        (bench.peer.directory / "warned").touch()
        await wait_for(lambda: events("approval"))
        card = events("approval")[0]
        assert card["status"] == "pending" and bench.peer.replies() == []
        assert entry.runner is not None
        assert await entry.runner.approve(card["request_id"], "deny", None) is True
        await wait_for(lambda: entry.session.control == "terminal")
    finally:
        await bench.stop()


async def test_allow_once_is_chosen_wherever_agy_lists_it(tmp_path: Path) -> None:
    asked = Asked(tmp_path)
    await asked.ask(options=[REJECT_ONCE, ALLOW_ONCE])
    assert asked.answers == [{"outcome": {"outcome": "selected", "optionId": "allow"}}]
    assert asked.cards() == []


class SetModeOnly:
    """An `agy` whose session options carry no `mode`: modes change through `session/set_mode`."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        self.asked.append((method, params))
        return {}


async def test_a_mode_agy_sets_only_through_set_mode_is_set_so(tmp_path: Path) -> None:
    agy: Any = SetModeOnly()
    started = Running(modes=[Choice(mode, mode) for mode in AGY_MODES], mode="default")
    chosen = settings.SessionSettings(
        entry_for(tmp_path, Recorder()).channel, settings.Wanted(mode=BYPASS.id)
    )
    await chosen.start(agy, "sid-1", started)
    assert agy.asked == [("session/set_mode", {"sessionId": "sid-1", "modeId": "accept-edits"})]
    assert chosen.bypassing


async def test_stop_in_bypass_permissions_ends_the_turn(tmp_path: Path) -> None:
    runner, recorder, peer = await in_bypass(tmp_path, turns=["hang"])
    await runner.send("Run the long command.")
    await wait_for(lambda: recorder.events("tool_call"))
    assert await runner.interrupt() is True
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()
    assert recorder.events("turn_completed")[0]["stop_reason"] == "interrupted"
    assert peer.calls("session/cancel") == [{"sessionId": SESSION}]
    assert recorder.events("approval") == []


async def test_without_allow_once_nothing_is_chosen_for_the_person(tmp_path: Path) -> None:
    """A64's fail-closed answer stands in Bypass permissions: never an option that grants more."""
    asked = Asked(tmp_path)
    always = {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"}
    await asked.ask(options=[always, REJECT_ONCE])
    assert asked.answers == [{"outcome": {"outcome": "cancelled"}}]
    assert asked.cards() == []


async def test_a_holder_the_device_could_not_make_way_for_still_brings_the_cards_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No turn of the device's runs, so it warns nobody, and its process cannot stop yet (here, a
    request holds the session): a turn `agy` starts before the next look still asks the person."""

    async def never(self: AntigravityService, seconds: float, step: Any) -> None:
        return None

    monkeypatch.setattr(AntigravityService, "_every", never)
    bench = Bench(tmp_path, monkeypatch, turns=["tool"])
    bench.peer.configure(list=bench.listed())
    await bench.start()
    try:
        await bench.hub.create(
            {"agent": "antigravity", "cwd": str(bench.work), "permission_mode": BYPASS.id}
        )
        entry = bench.entry()
        bench.holders = Holders([Holder(4242, SESSION, str(bench.work))])
        async with entry.lock:
            await bench.scan()
        assert entry.runner is not None and entry.session.control == "remote", "not let go"
        assert entry.runner is not None
        await entry.runner.send("List the files.")

        def cards() -> list[dict[str, Any]]:
            return [
                f["event"]
                for f in bench.frames
                if f.get("type") == "session.event" and f["event"]["kind"] == "approval"
            ]

        await wait_for(cards)
        assert cards()[0]["status"] == "pending" and bench.peer.replies() == []
        assert await entry.runner.approve(cards()[0]["request_id"], "deny", None) is True
    finally:
        await bench.stop()
