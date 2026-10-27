"""Amendment A55: the device's rename pipeline, the normaliser and the titles it feeds."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from rc_client.agents import registry
from rc_client.agents.grok.sessions import GrokSessionInfo
from rc_client.agents.registry import AGENT_IDS, DetectContext, RenameSpec, rename_in_agent
from rc_client.errors import RcError
from rc_client.models import UNSET, Session
from rc_client.sessions import titles
from rc_client.sessions.channel import SessionChannel
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.mirror import MirrorService
from rc_client.sessions.rename import normalise
from tests import agent_guard
from tests.helpers import FIXTURE_ROOT, load_fixture
from tests.rename_helpers import (
    RenameProbe,
    install_rename,
    remove_rename,
    rename_spec,
    summaries,
    title_events,
)
from tests.test_hub import FakeRunner, add_session, build_hub
from tests.test_titles import build_channel

LEDGER = "Ledger migration"


async def _until(ready: Callable[[], object], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the device never got that far")


# ------------------------------------------------------------------ the normaliser


def test_white_space_collapses_and_the_ends_are_trimmed() -> None:
    assert normalise("  Ledger \t migration\n\n 2 ") == "Ledger migration 2"
    # Python's own idea of white space, which includes the no-break and ideographic spaces.
    assert normalise("a\u00a0\u3000b") == "a b"


@pytest.mark.parametrize(
    "character",
    [
        "\x00",  # Cc: NUL
        "\x07",  # Cc: bell
        "\x1b",  # Cc: escape, which would start a key sequence in a terminal
        "\x7f",  # Cc: DEL
        "\x9f",  # Cc: a C1 control
        "\u200b",  # Cf: zero-width space, which is not white space to Python
        "\u200e",  # Cf: left-to-right mark
        "\u202e",  # Cf: right-to-left override
        "\ufeff",  # Cf: byte order mark
        "\u2028",  # Zl: line separator
        "\u2029",  # Zp: paragraph separator
    ],
)
def test_control_format_and_separator_characters_become_spaces(character: str) -> None:
    assert normalise(f"a{character}b") == "a b"
    assert normalise(f"a{character}{character}b") == "a b"
    with pytest.raises(RcError) as caught:
        normalise(character)
    assert (caught.value.code, caught.value.message) == ("bad_request", "title must not be empty")


def test_an_emoji_sequence_keeps_its_joiners() -> None:
    """A woman technologist is a woman, a zero-width joiner and a laptop."""
    technologist = "\U0001f469\u200d\U0001f4bb"
    assert normalise(technologist) == technologist
    assert normalise(f"  {technologist} on call ") == f"{technologist} on call"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert normalise(family) == family
    # A joiner is one code point, so a sequence counts every piece.
    assert len(normalise(technologist * 20)) == 60
    with pytest.raises(RcError):
        normalise(technologist * 21)


def test_a_non_joiner_keeps_the_words_of_persian_and_the_indic_scripts_whole() -> None:
    persian = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    assert normalise(persian) == persian
    assert normalise(f"{persian}  \u0628\u0627\u0634\u06af\u0627\u0647") == (
        f"{persian} \u0628\u0627\u0634\u06af\u0627\u0647"
    )


def test_the_joiners_are_the_only_format_characters_that_stay() -> None:
    for kept in ("\u200c", "\u200d"):
        assert normalise(f"a{kept}b") == f"a{kept}b"
    for spaced in ("\u200b", "\u200e", "\u200f", "\u2060", "\ufeff", "\u00ad"):
        assert normalise(f"a{spaced}b") == "a b"


def test_a_name_of_nothing_but_joiners_is_still_no_name() -> None:
    for blank in ("\u200d", "\u200c\u200d", "\u200d \u200c", " \u200d "):
        with pytest.raises(RcError) as caught:
            normalise(blank)
        assert (caught.value.code, caught.value.message) == (
            "bad_request",
            "title must not be empty",
        )


def test_a_lone_surrogate_is_a_space_because_utf8_cannot_carry_it() -> None:
    name = normalise("a\ud800b")
    assert name == "a b"
    assert name.encode("utf-8")


def test_nothing_is_rewritten_but_the_spacing() -> None:
    """No NFKC: full-width forms, CJK and emoji stay as they were typed."""
    assert (
        normalise("\uff21\uff22\uff23\u3000\u65e5\u672c\u8a9e \U0001f642")
        == "\uff21\uff22\uff23 \u65e5\u672c\u8a9e \U0001f642"
    )
    assert normalise("e\u0301") == "e\u0301"


def test_the_limit_counts_code_points() -> None:
    assert len(normalise("x" * 60)) == 60
    # One code point each, though a UTF-8 byte count says four and UTF-16 says two.
    assert len(normalise("\U0001f600" * 60)) == 60
    for too_long in ("x" * 61, "\U0001f600" * 61):
        with pytest.raises(RcError) as caught:
            normalise(too_long)
        assert caught.value.code == "bad_request"
        assert caught.value.message == "title is too long (max 60 characters)"


def test_an_empty_or_blank_name_is_refused() -> None:
    for blank in ("", "   ", "\n\t", "\u200b \u200b"):
        with pytest.raises(RcError) as caught:
            normalise(blank)
        assert (caught.value.code, caught.value.message) == (
            "bad_request",
            "title must not be empty",
        )


def test_a_name_that_is_not_text_is_refused() -> None:
    for wrong in (7, ["Ledger"], {"title": LEDGER}, True):
        with pytest.raises(RcError) as caught:
            normalise(wrong)
        assert (caught.value.code, caught.value.message) == (
            "bad_request",
            "title must be a string",
        )


# ------------------------------------------------------------ the pipeline in the hub


async def test_a_name_goes_to_the_agent_under_the_lock_and_the_reply_carries_what_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    # Claude suffixes a name another live session holds.
    probe = RenameProbe(held=f"{LEDGER} (2)")
    install_rename(monkeypatch, "claude", probe)

    result = await hub.set_options({"session_id": "sess-1", "title": "  Ledger\tmigration\n"})

    assert probe.names == [LEDGER]
    assert probe.locked == [True]
    assert not entry.lock.locked()
    assert result["session"]["title"] == f"{LEDGER} (2)"
    assert entry.session.title == f"{LEDGER} (2)"
    assert titles.pinned(registry_db, "sess-1")
    assert title_events(frames) == [f"{LEDGER} (2)"]
    # Nothing was drawn ahead of the agent's own answer.
    assert [summary["title"] for summary in summaries(frames)] == [f"{LEDGER} (2)"]
    registry_db.close()


async def test_a_rename_never_moves_the_session_up_the_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    entry.session.updated_at = 1_000
    install_rename(monkeypatch, "claude", RenameProbe())

    result = await hub.set_options({"session_id": "sess-1", "title": LEDGER})

    assert result["session"]["updated_at"] == 1_000
    assert [summary["updated_at"] for summary in summaries(frames)] == [1_000]
    assert registry_db.load_sessions()[0].updated_at == 1_000
    registry_db.close()


async def test_the_settings_of_a_frame_are_applied_before_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    seen: list[tuple[list[tuple[object, ...]], str | None]] = []

    async def hook(spec: RenameSpec) -> str:
        seen.append((list(runner.settings), spec.session.model))
        return spec.name

    install_rename(monkeypatch, "claude", hook)
    result = await hub.set_options({"session_id": "sess-1", "model": "opus", "title": LEDGER})

    assert seen == [([("opus", None, None, UNSET)], "opus")]
    assert result["session"]["model"] == "opus"
    assert result["session"]["title"] == LEDGER
    registry_db.close()


async def test_a_name_that_is_refused_applies_nothing_of_the_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    runner = entry.runner
    assert isinstance(runner, FakeRunner)
    probe = RenameProbe()
    install_rename(monkeypatch, "claude", probe)

    for title, message in (
        ("   ", "title must not be empty"),
        ("x" * 61, "title is too long (max 60 characters)"),
    ):
        with pytest.raises(RcError) as caught:
            await hub.set_options({"session_id": "sess-1", "model": "opus", "title": title})
        assert (caught.value.code, caught.value.message) == ("bad_request", message)

    assert probe.names == []
    assert runner.settings == []
    assert entry.session.model is None
    assert frames == []
    registry_db.close()


async def test_a_refusal_from_the_agent_leaves_the_title_and_the_pin_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    entry.session.title = "first words"
    refusal = RcError("conflict", "the terminal is busy; try again in a moment")
    install_rename(monkeypatch, "claude", RenameProbe(error=refusal))

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "title": LEDGER})

    assert caught.value is refusal
    assert entry.session.title == "first words"
    assert not titles.pinned(registry_db, "sess-1")
    assert title_events(frames) == []
    assert not entry.lock.locked()
    registry_db.close()


async def test_an_agent_without_the_hook_cannot_be_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    remove_rename(monkeypatch, "claude")

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": "sess-1", "title": LEDGER})

    assert (caught.value.code, caught.value.message) == (
        "unsupported",
        "claude sessions cannot be renamed from here",
    )
    assert entry.session.title == ""
    assert not titles.pinned(registry_db, "sess-1")
    assert title_events(frames) == []
    registry_db.close()


async def test_a_frame_without_a_title_renames_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    add_session(hub)
    probe = RenameProbe()
    install_rename(monkeypatch, "claude", probe)

    await hub.set_options({"session_id": "sess-1", "model": "opus"})
    await hub.set_options({"session_id": "sess-1", "model": "opus", "title": None})

    assert probe.names == []
    registry_db.close()


async def test_a_name_the_session_already_holds_is_answered_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    probe = RenameProbe()
    install_rename(monkeypatch, "claude", probe)

    await hub.set_options({"session_id": "sess-1", "title": LEDGER})
    again = await hub.set_options({"session_id": "sess-1", "title": f" {LEDGER}\n"})

    assert probe.names == [LEDGER]
    assert again["session"]["title"] == LEDGER
    assert not entry.lock.locked()
    registry_db.close()


async def test_a_generated_title_is_not_a_name_the_agent_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same words as the agent's own summary still have to be written as the user's name."""
    hub, _, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    probe = RenameProbe()
    install_rename(monkeypatch, "claude", probe)
    await titles.from_agent(entry.channel, LEDGER)
    assert not titles.pinned(registry_db, "sess-1")

    await hub.set_options({"session_id": "sess-1", "title": LEDGER})

    assert probe.names == [LEDGER]
    assert titles.pinned(registry_db, "sess-1")
    registry_db.close()


async def test_a_second_request_for_the_same_name_waits_instead_of_renaming_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    add_session(hub)
    probe = RenameProbe()
    probe.gate = asyncio.Event()
    install_rename(monkeypatch, "claude", probe)
    frame = {"session_id": "sess-1", "title": LEDGER}

    first = asyncio.create_task(hub.set_options(dict(frame)))
    await _until(lambda: probe.names)
    second = asyncio.create_task(hub.set_options(dict(frame)))
    await asyncio.sleep(0.05)
    assert probe.names == [LEDGER]
    assert not second.done()

    probe.gate.set()
    results = await asyncio.gather(first, second)

    assert probe.names == [LEDGER]
    assert [result["session"]["title"] for result in results] == [LEDGER, LEDGER]
    registry_db.close()


async def test_a_hook_that_answers_nothing_is_taken_to_hold_the_name_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    install_rename(monkeypatch, "claude", RenameProbe(held=""))

    await hub.set_options({"session_id": "sess-1", "title": LEDGER})

    assert entry.session.title == LEDGER
    registry_db.close()


async def test_a_session_deleted_while_the_agent_renames_it_stays_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_remove` does not take the lock, so the delete lands while the hook still works."""
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    probe = RenameProbe()
    probe.gate = asyncio.Event()
    install_rename(monkeypatch, "claude", probe)
    renaming = asyncio.create_task(hub.set_options({"session_id": "sess-1", "title": LEDGER}))
    await _until(lambda: probe.names)

    await hub.delete({"session_id": "sess-1"})
    frames.clear()
    probe.gate.set()

    with pytest.raises(RcError) as caught:
        await renaming
    assert caught.value.code == "not_found"
    # Nothing came back: no summary, no meta event, no row, no pin.
    assert frames == []
    assert registry_db.load_sessions() == []
    assert registry_db.history("sess-1", None, 10, None) == ([], False)
    assert not titles.pinned(registry_db, "sess-1")
    assert hub.entries == {}
    assert entry.session.title == ""
    registry_db.close()


async def test_a_session_deleted_while_a_rename_waits_for_it_is_never_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    probe = RenameProbe()
    install_rename(monkeypatch, "claude", probe)
    await entry.lock.acquire()
    renaming = asyncio.create_task(hub.set_options({"session_id": "sess-1", "title": LEDGER}))
    await asyncio.sleep(0.05)

    await hub.delete({"session_id": "sess-1"})
    frames.clear()
    entry.lock.release()

    with pytest.raises(RcError) as caught:
        await renaming
    assert caught.value.code == "not_found"
    assert probe.names == [], "the agent was never asked to rename what is gone"
    assert frames == []
    assert registry_db.load_sessions() == []
    registry_db.close()


async def test_what_the_agent_says_of_a_session_that_was_deleted_meanwhile_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    add_session(hub)
    probe = RenameProbe(error=RcError("timeout", "the terminal did not confirm the new name"))
    probe.gate = asyncio.Event()
    install_rename(monkeypatch, "claude", probe)
    renaming = asyncio.create_task(hub.set_options({"session_id": "sess-1", "title": LEDGER}))
    await _until(lambda: probe.names)

    await hub.delete({"session_id": "sess-1"})
    frames.clear()
    probe.gate.set()

    with pytest.raises(RcError) as caught:
        await renaming
    assert (caught.value.code, caught.value.message) == ("not_found", "unknown session sess-1")
    assert frames == []
    registry_db.close()


async def test_the_spec_carries_the_services_a_hook_may_need(
    tmp_path: Path,
) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    entry = add_session(hub)

    spec = rename_spec(hub, entry, LEDGER)

    assert spec.entry is entry
    assert spec.session is entry.session
    assert spec.channel is entry.channel
    assert spec.info.agent == "claude"
    assert spec.name == LEDGER
    assert spec.shared is hub.shared
    assert (spec.codex_daemon, spec.grok_leader, spec.pi_extensions) == (None, None, None)
    registry_db.close()


async def test_an_unknown_agent_is_unsupported(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    entry = add_session(hub)
    spec = rename_spec(hub, entry, LEDGER)
    with pytest.raises(RcError) as caught:
        await rename_in_agent("gemini", spec)
    assert caught.value.code == "unsupported"
    registry_db.close()


async def test_a_plugin_that_lists_the_rename_capability_has_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_guard.hide_agents(monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for agent in AGENT_IDS:
        plugin = registry.plugin(agent)
        info = await plugin.detect(DetectContext(codex_daemon_ready=False))
        if "rename" in info.capabilities:
            assert callable(getattr(plugin, "rename", None)), agent


def test_every_agent_can_be_renamed() -> None:
    """Grok lists the capability only while its leader is ready, but the hook is always there."""
    for agent in AGENT_IDS:
        assert callable(getattr(registry.plugin(agent), "rename", None)), agent


# ----------------------------------------------------------- the protocol fixtures


def test_the_rename_fixtures_agree_with_the_normaliser() -> None:
    for name in (
        "app/session.set.rename.json",
        "app/session.set.rename-max.json",
        "device/forwarded/session.set.rename.json",
    ):
        title = load_fixture(name)["title"]
        assert normalise(title) == title, name
    assert len(load_fixture("app/session.set.rename-max.json")["title"]) == 60

    invalid = FIXTURE_ROOT.parent / "fixtures_invalid"
    for name, message in (
        ("app__set_title_empty.json", "title must not be empty"),
        ("app__set_title_too_long.json", "title is too long (max 60 characters)"),
    ):
        title = json.loads((invalid / name).read_text(encoding="utf-8"))["title"]
        with pytest.raises(RcError) as caught:
            normalise(title)
        assert (caught.value.code, caught.value.message) == ("bad_request", message), name


async def test_the_forwarded_fixture_is_renamed_and_answered_as_the_reply_fixture_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = load_fixture("device/forwarded/session.set.rename.json")
    reply = load_fixture("app/reply.session.set.rename.json")["result"]["session"]
    hub, _, registry_db = build_hub(tmp_path)
    session = Session(
        session_id=request["session_id"], device_id="dev-1", agent="claude", cwd="/repo"
    )
    hub.registry.upsert_session(session)
    channel = SessionChannel(hub.registry, session, hub.publish)
    entry = SessionEntry(session=session, channel=channel)
    entry.runner = FakeRunner(channel)
    hub.entries[session.session_id] = entry
    # The effective name differs from the request: the agent held it under another name.
    install_rename(monkeypatch, "claude", RenameProbe(held=reply["title"]))

    result = await hub.set_options(request)

    assert reply["title"] != request["title"]
    assert result["session"]["title"] == reply["title"]
    assert set(reply) <= set(result["session"])
    assert Session.from_dict(reply).title == reply["title"]
    registry_db.close()


# ------------------------------------------------------------------ titles


async def test_a_name_the_user_chose_replaces_a_pinned_one_and_keeps_its_place(
    tmp_path: Path,
) -> None:
    channel, registry_db, frames = build_channel(tmp_path)
    channel.session.updated_at = 1_000
    await titles.from_user(channel, "Named in an app")
    assert titles.pinned(registry_db, "s1")

    # The agent reports a rename made in its own terminal: its record is the truth.
    assert await titles.from_user(channel, "Named in the terminal") is True

    assert channel.session.title == "Named in the terminal"
    assert titles.pinned(registry_db, "s1")
    assert channel.session.updated_at == 1_000
    assert [summary["updated_at"] for summary in summaries(frames)] == [1_000, 1_000]
    assert title_events(frames) == ["Named in an app", "Named in the terminal"]
    registry_db.close()


async def test_a_generated_title_still_moves_the_session_and_never_replaces_a_name(
    tmp_path: Path,
) -> None:
    channel, registry_db, _ = build_channel(tmp_path)
    channel.session.updated_at = 1_000
    assert await titles.from_agent(channel, "Create hello.txt file") is True
    assert channel.session.updated_at > 1_000

    await titles.from_user(channel, LEDGER)
    assert await titles.from_agent(channel, "A later reading") is False
    assert channel.session.title == LEDGER
    registry_db.close()


async def test_naming_a_session_ends_its_being_named_after_files(tmp_path: Path) -> None:
    channel, registry_db, _ = build_channel(tmp_path)
    await titles.from_prompt(channel, "", [{"name": "shot.png"}])
    assert channel.session.title == "shot.png"
    assert titles.named_after_files(registry_db, "s1")

    await titles.from_user(channel, LEDGER)

    assert not titles.named_after_files(registry_db, "s1")
    assert channel.session.title == LEDGER
    registry_db.close()


async def test_a_blank_name_neither_applies_nor_pins(tmp_path: Path) -> None:
    channel, registry_db, frames = build_channel(tmp_path)
    assert await titles.from_user(channel, "   ") is False
    assert not titles.pinned(registry_db, "s1")
    assert frames == []
    registry_db.close()


async def test_a_name_an_agent_reports_is_capped_like_every_other_source(tmp_path: Path) -> None:
    channel, registry_db, _ = build_channel(tmp_path)
    await titles.from_user(channel, "x" * 200)
    assert len(channel.session.title) == 60
    assert channel.session.title.endswith("…")
    registry_db.close()


# ----------------------------------------------------------------- Grok's manual titles


def _grok_info(session_id: str, title: str, manual: bool) -> GrokSessionInfo:
    return GrokSessionInfo(
        session_id=session_id,
        path=f"/nonexistent/{session_id}/updates.jsonl",
        cwd="/repo",
        title=title,
        title_is_manual=manual,
    )


async def test_a_title_grok_marks_manual_is_the_users_and_a_generated_one_is_not(
    tmp_path: Path,
) -> None:
    hub, frames, registry_db = build_hub(tmp_path)
    mirror = MirrorService(hub)

    await mirror._adopt_grok([_grok_info("g-generated", "Refactor the parser", False)])
    await mirror._adopt_grok([_grok_info("g-manual", LEDGER, True)])

    generated, manual = hub.entry("g-generated").session, hub.entry("g-manual").session
    assert generated.title == "Refactor the parser"
    assert not titles.pinned(registry_db, "g-generated")
    assert manual.title == LEDGER
    assert titles.pinned(registry_db, "g-manual")
    # The sessions were adopted with `updated_at` 0: a name is not activity, a summary is.
    assert manual.updated_at == 0
    assert generated.updated_at > 0
    assert {s["updated_at"] for s in summaries(frames) if s["session_id"] == "g-manual"} == {0}
    registry_db.close()


async def test_grok_reporting_a_manual_title_replaces_what_an_app_set(tmp_path: Path) -> None:
    hub, _, registry_db = build_hub(tmp_path)
    mirror = MirrorService(hub)
    for session_id in ("g-1", "g-2"):
        entry = hub.register_mirrored(
            Session(
                session_id=session_id,
                device_id="dev-1",
                agent="grok",
                cwd="/repo",
                origin="terminal",
            )
        )
        await titles.from_user(entry.channel, "Named in an app")

    await mirror._adopt_grok([_grok_info("g-1", "Generated afterwards", False)])
    await mirror._adopt_grok([_grok_info("g-2", "Renamed in the terminal", True)])

    assert hub.entry("g-1").session.title == "Named in an app"
    assert hub.entry("g-2").session.title == "Renamed in the terminal"
    registry_db.close()
