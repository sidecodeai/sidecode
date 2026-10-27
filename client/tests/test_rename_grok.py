"""Amendment A55: renaming a Grok session through the leader's `_x.ai/session/rename`.

The leader is the fake of `tests/fake_grok_agent.py` in a `leader` mode, a real child
process speaking real JSON-RPC. Its answer to the rename is not recorded from a real
Grok: the method and its fields come from the strings of Grok Build's binary, so what
these tests prove is the device's side of it. Every route goes through
`hub.set_options`, the frame an app sends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.agents.grok import naming
from rc_client.agents.grok import plugin as grok_plugin
from rc_client.agents.grok import runtime as grok_runtime
from rc_client.agents.grok import sessions as grok_sessions
from rc_client.agents.grok.acp import GrokError
from rc_client.agents.grok.service import GrokLeaderService
from rc_client.agents.registry import DetectContext
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.sessions import titles
from rc_client.sessions.hub import SessionEntry, SessionHub
from tests import test_grok_mirroring as mirroring
from tests.rename_helpers import summaries, title_events
from tests.test_grok_runner import Peer, Recorder, wait_for
from tests.test_grok_service import (
    CWD,
    SESSION,
    build,
    inject,
    leader_peer,
    register,
    unregister,
)
from tests.test_grok_service import home as home

LEDGER = "Ledger migration"
METHOD = "_x.ai/session/rename"
GENERATED = "Uninstalling the Codex CLI"


@dataclass
class Joined:
    """A leader the device has joined, and the session a terminal registered in it."""

    hub: SessionHub
    recorder: Recorder
    peer: Peer
    service: GrokLeaderService
    binary: str

    @property
    def entry(self) -> SessionEntry:
        return self.hub.entries[SESSION]


async def joined(home: Path, tmp_path: Path, **settings: Any) -> Joined:
    register(home)
    binary, peer = leader_peer(tmp_path, **settings)
    hub, recorder, service = build(tmp_path)
    await service.tick(binary)
    return Joined(hub, recorder, peer, service, binary)


async def rename(hub: SessionHub, title: str = LEDGER) -> dict[str, Any]:
    return await hub.set_options({"session_id": SESSION, "title": title})


async def announced(leader: Joined) -> None:
    """Wait until everything the leader sent before this call has been handled.

    Its answer to a request comes after every notification it sent earlier, so by
    the time that answer is read the handlers of those are running.
    """
    client = leader.service._client
    assert client is not None and client._agent is not None
    agent = client._agent
    await client.request("_x.ai/session/info", {"sessionId": SESSION}, timeout=5)
    await wait_for(lambda: not agent._tasks)


def stored_session(hub: SessionHub) -> SessionEntry:
    """A session the leader does not hold: one the mirror knows from Grok's own files."""
    return hub.register_mirrored(
        Session(session_id=SESSION, device_id="d1", agent="grok", cwd=CWD, control="none")
    )


# ------------------------------------------------------------------- the routes


async def test_a_session_a_terminal_holds_is_renamed_through_the_leader(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path)
    entry = leader.entry
    assert entry.session.control == "shared"
    entry.session.updated_at = 1_000
    leader.recorder.frames.clear()

    result = await rename(leader.hub, "  Ledger\tmigration\n")
    await announced(leader)

    assert leader.peer.calls(METHOD) == [{"sessionId": SESSION, "title": LEDGER, "cwd": CWD}]
    assert result["session"]["title"] == LEDGER
    assert result["session"]["updated_at"] == 1_000
    assert titles.pinned(leader.hub.registry, SESSION)
    # The leader announces the title it keeps, and that changes nothing.
    assert title_events(leader.recorder.frames) == [LEDGER]
    assert {summary["updated_at"] for summary in summaries(leader.recorder.frames)} == {1_000}
    await leader.service.stop()


async def test_a_session_nobody_is_in_is_renamed_through_the_leader_too(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path)
    unregister(home)
    await leader.service.tick(leader.binary)
    assert leader.entry.session.control == "none"

    result = await rename(leader.hub)

    assert leader.peer.calls(METHOD) == [{"sessionId": SESSION, "title": LEDGER, "cwd": CWD}]
    assert result["session"]["title"] == LEDGER
    await leader.service.stop()


async def test_a_name_the_leader_announces_without_the_flag_changes_nothing(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path, announce="unflagged")

    await rename(leader.hub)
    await announced(leader)

    assert leader.service._titles[SESSION] == grok_sessions.Title(LEDGER, manual=False)
    assert leader.entry.session.title == LEDGER
    assert title_events(leader.recorder.frames) == [LEDGER]
    await leader.service.stop()


# --------------------------------------------------------------------- refusals


async def test_grok_without_leader_mode_cannot_be_renamed_from_here(
    home: Path, tmp_path: Path
) -> None:
    (home / "config.toml").write_text("[cli]\nuse_leader = false\n", encoding="utf-8")
    hub, _, service = build(tmp_path)
    entry = stored_session(hub)

    with pytest.raises(RcError) as caught:
        await rename(hub)

    assert (caught.value.code, caught.value.message) == ("unsupported", naming.NOT_JOINED)
    assert entry.session.title == ""
    assert not titles.pinned(hub.registry, SESSION)
    await service.stop()


async def test_a_device_with_no_leader_service_cannot_rename(home: Path, tmp_path: Path) -> None:
    hub, _, _ = build(tmp_path)
    hub.grok_leader = None
    stored_session(hub)

    with pytest.raises(RcError) as caught:
        await rename(hub)

    assert (caught.value.code, caught.value.message) == ("unsupported", naming.NOT_JOINED)


async def test_a_leader_that_is_gone_cannot_be_asked(home: Path, tmp_path: Path) -> None:
    leader = await joined(home, tmp_path)
    client = leader.service._client
    assert client is not None and client._agent is not None and client._agent._process is not None
    client._agent._process.kill()
    await wait_for(lambda: not leader.service.ready, timeout=5)

    with pytest.raises(RcError) as caught:
        await rename(leader.hub)

    assert (caught.value.code, caught.value.message) == ("unsupported", naming.NOT_JOINED)
    assert leader.peer.calls(METHOD) == []
    await leader.service.stop()


async def test_a_leader_that_dies_in_the_middle_of_it_is_gone_as_well(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path, rename="die")
    before = leader.entry.session.title

    with pytest.raises(RcError) as caught:
        await rename(leader.hub)

    assert (caught.value.code, caught.value.message) == ("unsupported", naming.NOT_JOINED)
    assert leader.peer.calls(METHOD) == [{"sessionId": SESSION, "title": LEDGER, "cwd": CWD}]
    assert leader.entry.session.title == before
    assert not titles.pinned(leader.hub.registry, SESSION)
    await leader.service.stop()


NAME_REFUSED = ("bad_request", "grok does not accept this name")
LEFT_TO_THE_TERMINAL = ("unsupported", naming.NOT_TAKEN)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        # A name it will not take is the person's to change.
        ("blank", NAME_REFUSED),
        ("too-long", NAME_REFUSED),
        ("invalid-params", NAME_REFUSED),
        # A session it does not hold, a Grok that has no such method, and a failure
        # inside it that merely mentions the title are the terminal's.
        ("no-session", LEFT_TO_THE_TERMINAL),
        ("unknown", LEFT_TO_THE_TERMINAL),
        ("write-error", LEFT_TO_THE_TERMINAL),
    ],
)
async def test_what_grok_refuses_is_said_in_our_words(
    home: Path, tmp_path: Path, scenario: str, expected: tuple[str, str]
) -> None:
    leader = await joined(home, tmp_path, rename=scenario)
    before = leader.entry.session.title
    leader.recorder.frames.clear()

    with pytest.raises(RcError) as caught:
        await rename(leader.hub)

    assert (caught.value.code, caught.value.message) == expected
    assert leader.entry.session.title == before
    assert not titles.pinned(leader.hub.registry, SESSION)
    assert title_events(leader.recorder.frames) == []
    await leader.service.stop()


@pytest.mark.parametrize(
    ("code", "message", "detail", "expected"),
    [
        # Refused fields, whatever words come with them.
        (-32602, "Invalid params", None, NAME_REFUSED),
        (-32602, "No active session", None, NAME_REFUSED),
        # The words of a name it will not take, in the message or in `data`, in any case.
        (-32603, "Internal error", "title must not be blank", NAME_REFUSED),
        (-32603, "Title Must Not Be Blank", None, NAME_REFUSED),
        (-32603, "Internal error", "title too long (max 40 characters)", NAME_REFUSED),
        (-32603, "Internal error", "title too large", NAME_REFUSED),
        (None, "title too long", None, NAME_REFUSED),
        # Everything else is left to the terminal, a title in its words or not.
        (-32601, "Method not found", None, LEFT_TO_THE_TERMINAL),
        (-32603, "Internal error", "No active session", LEFT_TO_THE_TERMINAL),
        (
            -32603,
            "Internal error",
            "failed to write the title: permission denied",
            LEFT_TO_THE_TERMINAL,
        ),
        (-32603, "Internal error", "the session title store is locked", LEFT_TO_THE_TERMINAL),
        (-32603, "Internal error", None, LEFT_TO_THE_TERMINAL),
        (None, "grok error", None, LEFT_TO_THE_TERMINAL),
    ],
)
def test_the_leaders_refusal_reads_as_the_names_only_when_it_says_so(
    code: int | None, message: str, detail: str | None, expected: tuple[str, str]
) -> None:
    refusal = naming.refusal(GrokError(message, code, detail))

    assert (refusal.code, refusal.message) == expected


async def test_a_leader_that_never_answers_times_out(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(naming, "TIMEOUT", 0.2)
    leader = await joined(home, tmp_path, rename="silent")

    with pytest.raises(RcError) as caught:
        await rename(leader.hub)

    assert caught.value.code == "timeout"
    assert not titles.pinned(leader.hub.registry, SESSION)
    await leader.service.stop()


async def test_a_rename_replaces_the_title_the_service_remembers_for_the_session(
    home: Path, tmp_path: Path
) -> None:
    """A leader that announces nothing for a rename leaves an older manual title in the memory."""
    leader = await joined(home, tmp_path, announce="none")
    older = "Chosen in the terminal"
    inject(
        leader.peer,
        "10-changed.json",
        {
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "upserted": [{"sessionId": SESSION, "title": older, "titleIsManual": True}],
                "removed": [],
            },
        },
    )
    await wait_for(lambda: leader.service._titles.get(SESSION) == grok_sessions.Title(older, True))
    assert leader.entry.session.title == older

    await rename(leader.hub)

    assert leader.service._titles[SESSION] == grok_sessions.Title(LEDGER, manual=True)
    # What a later adoption of the session reads.
    await leader.service._name(leader.entry)
    assert leader.entry.session.title == LEDGER
    await leader.service.stop()


async def test_a_rename_the_leader_refused_leaves_the_remembered_title_alone(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path, rename="blank")
    remembered = grok_sessions.Title("Chosen in the terminal", manual=True)
    leader.service._titles[SESSION] = remembered

    with pytest.raises(RcError):
        await rename(leader.hub)

    assert leader.service._titles[SESSION] == remembered
    await leader.service.stop()


def test_the_requests_fields_are_spelled_in_one_place() -> None:
    assert naming.METHOD == METHOD
    assert naming.params("s-1", "/repo", "A name") == {
        "sessionId": "s-1",
        "title": "A name",
        "cwd": "/repo",
    }


# --------------------------------------------------------- titles Grok reports


async def test_a_title_the_leader_marks_manual_replaces_a_name_an_app_gave(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path)
    entry = leader.entry
    await titles.from_user(entry.channel, "Chosen on the phone")
    entry.session.updated_at = 1_000
    leader.recorder.frames.clear()

    inject(
        leader.peer,
        "10-changed.json",
        {
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "upserted": [{"sessionId": SESSION, "title": LEDGER, "titleIsManual": True}],
                "removed": [],
            },
        },
    )
    await wait_for(lambda: entry.session.title == LEDGER)

    assert entry.session.updated_at == 1_000
    assert titles.pinned(leader.hub.registry, SESSION)
    assert title_events(leader.recorder.frames) == [LEDGER]
    await leader.service.stop()


async def test_a_title_grok_wrote_for_itself_never_replaces_a_name(
    home: Path, tmp_path: Path
) -> None:
    leader = await joined(home, tmp_path)
    entry = leader.entry
    await titles.from_user(entry.channel, "Chosen on the phone")

    inject(
        leader.peer,
        "10-changed.json",
        {
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {"upserted": [{"sessionId": SESSION, "title": "A summary"}], "removed": []},
        },
    )
    await wait_for(lambda: leader.service._titles.get(SESSION) == grok_sessions.Title("A summary"))

    assert entry.session.title == "Chosen on the phone"
    await leader.service.stop()


def write_summary(**fields: Any) -> None:
    """The `summary.json` Grok keeps beside a session."""
    directory = grok_sessions.session_dir(SESSION, CWD)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(
        json.dumps({"info": {"id": SESSION, "cwd": CWD}, **fields}), encoding="utf-8"
    )


async def test_a_title_a_terminal_chose_is_the_users_when_the_session_is_joined(
    home: Path, tmp_path: Path
) -> None:
    write_summary(generated_title=LEDGER, title_is_manual=True)
    register(home)
    binary, _ = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    earlier = stored_session(hub)
    await titles.from_user(earlier.channel, "Chosen on the phone")

    await service.tick(binary)

    assert hub.entries[SESSION].session.title == LEDGER
    await service.stop()


async def test_a_title_grok_generated_does_not_replace_a_name_when_the_session_is_joined(
    home: Path, tmp_path: Path
) -> None:
    write_summary(generated_title=GENERATED)
    register(home)
    binary, _ = leader_peer(tmp_path)
    hub, _, service = build(tmp_path)
    earlier = stored_session(hub)
    await titles.from_user(earlier.channel, "Chosen on the phone")

    await service.tick(binary)

    assert hub.entries[SESSION].session.title == "Chosen on the phone"
    await service.stop()


async def test_the_leaders_own_index_says_which_titles_are_manual(
    home: Path, tmp_path: Path
) -> None:
    register(home)
    binary, _ = leader_peer(tmp_path, titles={SESSION: LEDGER}, manual=[SESSION])
    hub, _, service = build(tmp_path)
    earlier = stored_session(hub)
    await titles.from_user(earlier.channel, "Chosen on the phone")

    await service.tick(binary)

    assert hub.entries[SESSION].session.title == LEDGER
    assert service._titles[SESSION] == grok_sessions.Title(LEDGER, manual=True)
    await service.stop()


# ----------------------------------------------------- what discovery reads


def test_discovery_reads_whether_a_person_chose_the_title(home: Path) -> None:
    mirroring.write_session(
        home, summary={"info": {"cwd": CWD}, "generated_title": LEDGER, "title_is_manual": True}
    )
    found = grok_sessions.discover()
    assert [(info.title, info.title_is_manual) for info in found] == [(LEDGER, True)]


@pytest.mark.parametrize(
    "summary",
    [
        {"generated_title": GENERATED},
        {"generated_title": GENERATED, "title_is_manual": False},
        {"generated_title": GENERATED, "title_is_manual": "true"},
        {"title_is_manual": True},
    ],
)
def test_a_title_nobody_chose_is_not_manual(home: Path, summary: dict[str, Any]) -> None:
    mirroring.write_session(home, summary={"info": {"cwd": CWD}, **summary})
    assert [info.title_is_manual for info in grok_sessions.discover()] == [False]


async def test_the_mirror_takes_a_manual_title_as_the_users(home: Path, tmp_path: Path) -> None:
    mirroring.write_session(
        home, summary={"info": {"cwd": CWD}, "generated_title": LEDGER, "title_is_manual": True}
    )
    mirror, hub, _ = mirroring.build(tmp_path)

    await mirror.scan_once()

    assert hub.entries[SESSION].session.title == LEDGER
    assert titles.pinned(hub.registry, SESSION)


async def test_the_mirror_takes_a_generated_title_as_the_agents(home: Path, tmp_path: Path) -> None:
    mirroring.write_session(home, summary={"info": {"cwd": CWD}, "generated_title": GENERATED})
    mirror, hub, _ = mirroring.build(tmp_path)

    await mirror.scan_once()

    assert hub.entries[SESSION].session.title == GENERATED
    assert not titles.pinned(hub.registry, SESSION)


# ------------------------------------------------------------- the capability


async def test_detection_lists_rename_last_while_the_leader_is_ready(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With a binary found, detection runs `grok --version`, so none may be found.
    monkeypatch.setattr(grok_runtime, "resolve_binary", lambda: None)

    info = await grok_plugin.detect(DetectContext())

    assert info.attach_ready is True
    assert info.capabilities[-1] == "rename"
    assert callable(getattr(registry.plugin("grok"), "rename", None))


@pytest.mark.parametrize(
    ("config", "sandbox"),
    [
        ("[cli]\nuse_leader = false\n", None),
        ("", None),
        ("[cli]\nuse_leader = true\n", "strict"),
    ],
    ids=["leader-mode-off", "never-configured", "sandbox-profile-on"],
)
async def test_rename_is_not_listed_while_attach_is_not_ready(
    home: Path, monkeypatch: pytest.MonkeyPatch, config: str, sandbox: str | None
) -> None:
    monkeypatch.setattr(grok_runtime, "resolve_binary", lambda: None)
    (home / "config.toml").write_text(config, encoding="utf-8")
    if sandbox is not None:
        monkeypatch.setenv("GROK_SANDBOX", sandbox)

    info = await grok_plugin.detect(DetectContext())

    assert info.attach_ready is False
    assert "rename" not in info.capabilities
    # The hook is there all the same, and refuses at call time.
    assert callable(getattr(registry.plugin("grok"), "rename", None))
