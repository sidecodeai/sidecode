"""Amendments A40 and A42: the device types into an attached Claude terminal."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from rc_client.agents.claude import plugin as claude_plugin
from rc_client.agents.claude.commands import COMPACT
from rc_client.channel import paths, shim
from rc_client.channel.screen import Screen
from rc_client.errors import RcError
from rc_client.models import AgentInfo, Choice
from rc_client.registry import Registry
from rc_client.sessions import typist
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.ptys import PtyLink, TerminalState
from rc_client.sessions.queue import hold
from rc_client.sessions.shared import pending_item
from rc_client.sessions.typist import HIGHLIGHT, read_picker
from tests.helpers import drained

from .test_shared_control import FakeAttachment, FakeHookQuestion

REQUEST = "5c1d7e2a-9b3f-4a8c-8d6e-0f1a2b3c4d5e"
# The picker as Claude Code 2.1.278 draws it.
PICKER_ROWS = ("Default (recommended)", "Opus (1M context)", "Fable", "Sonnet", "Haiku")
PICKER_FOOTER = "Enter to set as default · s to use this session only · Esc to cancel"
SLIDER_FOOTER = "←/→ to adjust · Enter to confirm · s for this session only · Esc to cancel"
SLIDER_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultracode")
# Claude Code 2.1.299: five levels, fewer for a capped model, and Ultracode
# apart as a toggle on Tab.
NEW_SLIDER_LEVELS = ("low", "medium", "high", "xhigh", "max")
TAB = "\t"
# The frames 2.1.299 drew for `/effort`, recorded live through the proxy.
RECORDED_SLIDER = Path(__file__).parent / "fixtures" / "claude" / "effort-slider-2.1.299.raw"


def recorded_slider() -> str:
    """What the proxy's screen reader made of them: a footer redrawn in part."""
    screen = Screen()
    screen.feed(RECORDED_SLIDER.read_bytes())
    return screen.text()


class _NoSocket:
    """Stands in for the stream a real link writes its frames on."""

    def close(self) -> None:
        return None


class FakeTerminal(PtyLink):
    """A terminal that walks its pickers exactly as Claude Code's do."""

    def __init__(
        self,
        *,
        draft: int = 0,
        idle_for: float = 5.0,
        opens: bool = True,
        asks_to_switch: bool = False,
        new_slider: bool = False,
        levels: int = len(NEW_SLIDER_LEVELS),
        level: int = 4,
    ) -> None:
        super().__init__(4242, cast(asyncio.StreamWriter, _NoSocket()))
        self.typed: list[str] = []
        self.draft = draft
        self.idle_for = idle_for
        self.opens = opens
        # A conversation with cached history asks once more before switching.
        self.asks_to_switch = asks_to_switch
        self.new_slider = new_slider
        self.levels = SLIDER_LEVELS if not new_slider else NEW_SLIDER_LEVELS[:levels]
        self.showing = "none"
        self.row = 5
        self.level = level
        self.ultracode = False
        self.applied_model: str | None = None
        self.applied_effort: str | None = None

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        self.typed.append(data)
        self._press(data)
        return TerminalState(self.draft, self.idle_for)

    async def screen(self) -> TerminalState:
        return TerminalState(self.draft, self.idle_for, self._drawn())

    async def state(self) -> TerminalState:
        return TerminalState(self.draft, self.idle_for)

    def _press(self, data: str) -> None:
        if data == typist.MODEL_COMMAND + typist.ENTER:
            self.showing = "model" if self.opens else "none"
        elif data == typist.EFFORT_COMMAND + typist.ENTER:
            self.showing = "effort" if self.opens else "none"
        elif data == typist.ESCAPE:
            self.showing = "none"
        elif data == typist.ENTER and self.showing == "switch":
            self.applied_model = PICKER_ROWS[self.row - 1]
            self.showing = "none"
        elif data == typist.ENTER and self.showing == "change":
            self.applied_effort = self.levels[self.level]
            self.showing = "none"
        elif data == typist.THIS_SESSION:
            if self.showing in {"model", "effort"} and self.asks_to_switch:
                self.showing = "switch" if self.showing == "model" else "change"
            elif self.showing == "model":
                self.applied_model = PICKER_ROWS[self.row - 1]
                self.showing = "none"
            elif self.showing == "effort":
                self.applied_effort = self.levels[self.level]
                self.showing = "none"
        elif data == TAB and self.showing == "effort" and self.new_slider:
            self.ultracode = not self.ultracode
        elif self.showing == "model":
            moved = self.row + data.count(typist.DOWN) - data.count(typist.UP)
            self.row = min(len(PICKER_ROWS), max(1, moved))
        elif self.showing == "effort":
            # The slider stops at its ends, one key at a time, which is what
            # makes "all the way left, then right" land where it is told.
            self.level = max(0, self.level - data.count(typist.LEFT))
            self.level = min(len(self.levels) - 1, self.level + data.count(typist.RIGHT))

    def _drawn(self) -> str:
        if self.showing == "model":
            rows = "\n".join(
                f"{HIGHLIGHT if number == self.row else ' '} {number}. {label}"
                for number, label in enumerate(PICKER_ROWS, start=1)
            )
            return f"Select model\n{rows}\n{PICKER_FOOTER}\n"
        if self.showing == "effort":
            if self.new_slider:
                return recorded_slider()
            return f"Effort\n{'─' * 20}\n{SLIDER_FOOTER}\n"
        if self.showing == "change":
            # Drawn over the slider, so its title may come through with a gap.
            return (
                "Change ef ort level?\n Your next response will be slower and use more tokens\n"
                f" {HIGHLIGHT} 1. Yes, switch to {self.levels[self.level]}\n   2. No, go back\n"
            )
        if self.showing == "switch":
            return (
                "Switch model?\n Your next response will be slower and use more tokens\n"
                f" {HIGHLIGHT} 1. Yes, switch to {PICKER_ROWS[self.row - 1]}\n   2. No, go back\n"
            )
        return "> \n"


def agents(stops: bool = True) -> list[AgentInfo]:
    """This device's Claude, with `stops` for what A42's Escape needs."""
    return [
        AgentInfo(
            agent="claude",
            available=True,
            path="/bin/claude",
            models=[Choice(name, name.title()) for name in typist.MODEL_ROWS],
            default_model="default",
            efforts=[Choice(level, level.title()) for level in typist.EFFORT_ORDER],
            permission_modes=[Choice("default", "Ask before edits"), Choice("plan", "Plan mode")],
            capabilities=["takeover", "interrupt", "queue", "history", "commands"],
            attach="channel",
            attach_ready=True,
            shared_interrupt=stops,
            shared_settings=True,
            shared_settings_keys=["model", "effort"],
        )
    ]


class Harness:
    """A hub with one attached session and the terminal it runs in."""

    def __init__(self, tmp_path: Path, stops: bool = True) -> None:
        self.frames: list[dict[str, Any]] = []
        self.registry = Registry(tmp_path / "state.sqlite3")

        async def publish(frame: dict[str, Any]) -> None:
            self.frames.append(frame)

        self.hub = SessionHub(self.registry, publish, "dev-1", lambda: agents(stops))
        self.attachment = FakeAttachment()
        self.terminal = FakeTerminal()

    async def attach(self, terminal: FakeTerminal | None = None) -> SessionEntry:
        if terminal is not None:
            self.terminal = terminal
        await self.hub.attach_registered(self.attachment)
        self.hub.ptys.add(self.terminal)
        return self.hub.entry("sess-1")

    async def confirm(self, entry: SessionEntry, command: str, answer: str) -> None:
        """What the tailer reports once the CLI has written both rows."""
        await self.hub.shared.typed_command(entry, command)
        await self.hub.shared.command_output(entry, f"<local-command-stdout>{answer}")

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


async def _until(ready: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if ready():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the device never got that far")


# ------------------------------------------------------------------ reading


def test_the_picker_is_read_from_the_last_frame_the_terminal_drew() -> None:
    first = f"{HIGHLIGHT} 1. Default (recommended)\n  2. Opus (1M context)\n  3. Fable\n"
    second = f"  1. Default (recommended)\n  2. Opus (1M context)\n{HIGHLIGHT} 3. Fable\n"
    picker = read_picker(first + second)
    assert picker.rows == {1: "Default", 2: "Opus", 3: "Fable"}
    assert picker.current == "Fable"
    assert picker.row_for("Fable") == 3
    assert picker.row_for("Haiku") is None
    assert read_picker("nothing here").rows == {}


def test_a_moved_highlight_is_read_from_a_row_redrawn_without_its_number() -> None:
    """What Claude Code 2.1.278 redraws for Down: the row, and no number."""
    frame = (
        f" {HIGHLIGHT} 1. Default (recommended) \u2714 Use the default model\n"
        " 2. Opus (1M context) Opus 5 with 1M context\n"
        " 3. Fable Fable 5.1 \u00b7 Most capable\n"
    )
    picker = read_picker(f"{frame}\n{HIGHLIGHT} Opus (1M context)\n")
    assert (picker.current, picker.row_for(picker.current)) == ("Opus", 2)
    # The composer's own prompt is not a row, whatever is typed into it.
    assert read_picker(f"{frame}\n{HIGHLIGHT} /model\n").current == "Default"


# -------------------------------------------------------------- the scripts


async def test_the_model_picker_is_walked_and_the_reply_waits_for_the_transcript(
    harness: Harness,
) -> None:
    entry = await harness.attach()
    terminal = harness.terminal
    change = asyncio.create_task(
        harness.hub.set_options({"session_id": "sess-1", "model": "sonnet"})
    )
    await _until(lambda: terminal.applied_model is not None)

    # Haiku is row five and Sonnet is row four: one press of Up, then `s`.
    assert terminal.typed == ["/model\r", typist.UP, "s"]
    assert terminal.applied_model == "Sonnet"
    # Nothing is said until the CLI has written what it did.
    assert not change.done()
    assert entry.session.model != "sonnet"

    await harness.confirm(entry, "/model", "Set model to `Sonnet 4.5` for this session")
    result = await asyncio.wait_for(change, timeout=2)
    assert result["session"]["model"] == "sonnet"
    assert entry.session.model == "sonnet"
    assert harness.events("meta")[-1]["model"] == "sonnet"
    # A settings change is not a message: no bubble for the keystrokes.
    assert harness.events("user_message") == []


async def test_a_cached_conversation_asks_once_more_and_the_device_says_yes(
    harness: Harness,
) -> None:
    """With history behind it the CLI puts up "Switch model?" after
    `s`, yes highlighted; the device presses Enter and the change goes on."""
    harness.terminal.asks_to_switch = True
    entry = await harness.attach()
    terminal = harness.terminal
    change = asyncio.create_task(
        harness.hub.set_options({"session_id": "sess-1", "model": "sonnet"})
    )
    await _until(lambda: terminal.applied_model is not None)
    assert terminal.typed == ["/model\r", typist.UP, "s", typist.ENTER]
    assert terminal.applied_model == "Sonnet"

    await harness.confirm(entry, "/model", "Set model to `Sonnet 4.5` for this session")
    await asyncio.wait_for(change, timeout=2)
    assert entry.session.model == "sonnet"


async def test_the_effort_slider_is_walked_from_the_left(harness: Harness) -> None:
    entry = await harness.attach()
    terminal = harness.terminal
    change = asyncio.create_task(
        harness.hub.set_options({"session_id": "sess-1", "effort": "high"})
    )
    await _until(lambda: terminal.applied_effort is not None)
    assert terminal.typed == ["/effort\r", typist.LEFT * 6 + typist.RIGHT * 2, "s"]
    assert terminal.applied_effort == "high"

    await harness.confirm(entry, "/effort", "Set effort level to high for this session")
    await asyncio.wait_for(change, timeout=2)
    assert entry.session.effort == "high"


def test_the_recorded_2_1_299_slider_is_known_though_its_footer_has_gaps() -> None:
    drawn = recorded_slider()
    # The redraw skipped cells the screen already held, so the old hint is cut.
    assert "s for this se sion only" in drawn
    assert typist.EFFORT_HINTS[1] not in drawn
    assert any(hint in drawn for hint in typist.EFFORT_HINTS)


async def test_the_2_1_299_slider_is_opened_from_what_the_terminal_drew(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new slider: five levels, Ultracode on Tab, and a footer with gaps."""
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.05)
    entry = await harness.attach(FakeTerminal(new_slider=True, level=1))
    terminal = harness.terminal
    change = asyncio.create_task(
        harness.hub.set_options({"session_id": "sess-1", "effort": "high"})
    )
    await _until(lambda: terminal.applied_effort is not None)
    assert terminal.typed == ["/effort\r", typist.LEFT * 6 + typist.RIGHT * 2, "s"]
    assert terminal.applied_effort == "high"
    assert terminal.ultracode is False

    await harness.confirm(entry, "/effort", "Set effort level to high (this session only): …")
    await asyncio.wait_for(change, timeout=2)
    assert entry.session.effort == "high"


@pytest.mark.parametrize("start", [0, 4], ids=["from-low", "from-max"])
@pytest.mark.parametrize("level", typist.EFFORT_ORDER)
async def test_every_level_of_the_2_1_299_slider_is_reached_from_either_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: int, level: str
) -> None:
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.05)
    harness = Harness(tmp_path)
    entry = await harness.attach(FakeTerminal(new_slider=True, level=start))
    terminal = harness.terminal
    change = asyncio.create_task(harness.hub.set_options({"session_id": "sess-1", "effort": level}))
    await _until(lambda: terminal.applied_effort is not None)
    await harness.confirm(entry, "/effort", f"Set effort level to {level} (this session only)")
    await asyncio.wait_for(change, timeout=2)
    assert (terminal.applied_effort, entry.session.effort) == (level, level)
    assert TAB not in "".join(terminal.typed)
    harness.close()


async def test_a_cached_conversation_asks_before_a_new_effort_and_the_device_says_yes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.1.299 asks "Change effort level?" with yes highlighted, as `/model` does."""
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.5)
    entry = await harness.attach(FakeTerminal(new_slider=True, level=1, asks_to_switch=True))
    terminal = harness.terminal
    change = asyncio.create_task(harness.hub.set_options({"session_id": "sess-1", "effort": "max"}))
    await _until(lambda: terminal.applied_effort is not None)
    assert terminal.typed == ["/effort\r", typist.LEFT * 6 + typist.RIGHT * 4, "s", typist.ENTER]
    assert terminal.applied_effort == "max"

    await harness.confirm(entry, "/effort", "Set effort level to max (this session only)")
    await asyncio.wait_for(change, timeout=2)
    assert entry.session.effort == "max"


async def test_a_capped_slider_sets_the_nearest_level_and_the_session_says_which(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model capped at `high` draws three levels; its answer names the one it took."""
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.05)
    entry = await harness.attach(FakeTerminal(new_slider=True, levels=3, level=0))
    terminal = harness.terminal
    change = asyncio.create_task(harness.hub.set_options({"session_id": "sess-1", "effort": "max"}))
    await _until(lambda: terminal.applied_effort is not None)
    assert terminal.applied_effort == "high"

    await harness.confirm(entry, "/effort", "Set effort level to high (this session only)")
    reply = await asyncio.wait_for(change, timeout=2)
    assert reply["session"]["effort"] == "high"
    assert entry.session.effort == "high"
    assert harness.events("meta")[-1]["effort"] == "high"
    assert typist.ESCAPE not in terminal.typed, "the change took, so nothing is closed"


async def test_a_cap_in_the_person_s_settings_is_read_the_way_the_cli_words_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code 2.1.303 answers a level over that cap with the one it set instead."""
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.05)
    entry = await harness.attach(FakeTerminal(new_slider=True, level=0))
    change = asyncio.create_task(harness.hub.set_options({"session_id": "sess-1", "effort": "max"}))
    await _until(lambda: harness.terminal.applied_effort is not None)
    await harness.confirm(
        entry,
        "/effort",
        "Effort 'max' exceeds the cap for claude-opus-5 set by your settings or organization; "
        "set to 'high' instead (this session only): Comprehensive implementation",
    )
    reply = await asyncio.wait_for(change, timeout=2)
    assert (reply["session"]["effort"], entry.session.effort) == ("high", "high")


async def test_an_effort_answer_naming_no_level_we_know_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.05)
    entry = await harness.attach(FakeTerminal(new_slider=True, level=0))
    change = asyncio.create_task(harness.hub.shared.set_settings(entry, None, "low"))
    await _until(lambda: harness.terminal.applied_effort is not None)
    await harness.confirm(entry, "/effort", "Set effort level to ultracode (this session only)")
    with pytest.raises(RcError) as caught:
        await asyncio.wait_for(change, timeout=2)
    assert (caught.value.code, caught.value.message) == ("conflict", typist.REFUSED)
    assert entry.session.effort is None


async def test_the_person_at_the_keyboard_always_wins(harness: Harness) -> None:
    """Three ways a terminal is busy, and the same refusal for each."""
    entry = await harness.attach()
    state = entry.shared
    assert state is not None

    state.running = True
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options({"session_id": "sess-1", "model": "opus"})
    assert (caught.value.code, caught.value.message) == ("conflict", typist.BUSY)
    state.running = False

    for terminal in (FakeTerminal(draft=3), FakeTerminal(idle_for=0.5)):
        harness.hub.ptys.add(terminal)
        with pytest.raises(RcError) as caught:
            await harness.hub.set_options({"session_id": "sess-1", "model": "opus"})
        assert (caught.value.code, caught.value.message) == ("conflict", typist.BUSY)
        assert terminal.typed == []
    assert entry.session.model != "opus"


async def test_a_picker_that_never_opens_is_closed_again_and_refused(harness: Harness) -> None:
    entry = await harness.attach(FakeTerminal(opens=False))
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options({"session_id": "sess-1", "model": "opus"})
    assert caught.value.code == "conflict"
    assert harness.terminal.typed[-1] == typist.ESCAPE
    assert entry.shared is not None and entry.shared.typed is None
    # The terminal is free again straight away.
    assert entry.shared.injectable is True


async def test_a_change_the_transcript_never_confirms_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(typist, "CONFIRM_TIMEOUT", 0.05)
    await harness.attach()
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options({"session_id": "sess-1", "model": "opus"})
    assert (caught.value.code, caught.value.message) == ("conflict", typist.REFUSED)
    assert harness.terminal.typed[-1] == typist.ESCAPE


async def test_a_command_that_answered_with_something_else_is_refused(harness: Harness) -> None:
    entry = await harness.attach()
    change = asyncio.create_task(harness.hub.set_options({"session_id": "sess-1", "model": "opus"}))
    await _until(lambda: harness.terminal.applied_model is not None)
    await harness.confirm(entry, "/model", "Cancelled")
    with pytest.raises(RcError) as caught:
        await asyncio.wait_for(change, timeout=2)
    assert (caught.value.code, caught.value.message) == ("conflict", typist.REFUSED)
    assert entry.session.model != "opus"


async def test_a_terminal_the_device_cannot_type_into_says_so(harness: Harness) -> None:
    entry = await harness.attach()
    harness.hub.ptys.clear()
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options({"session_id": "sess-1", "model": "opus"})
    assert caught.value.code == "conflict"
    assert "start it again" in caught.value.message

    # And Stop is the same keystroke route, so it is refused the same way (A42).
    state = entry.shared
    assert state is not None
    state.running = True
    with pytest.raises(RcError) as caught:
        await harness.hub.stop({"session_id": "sess-1"})
    assert caught.value.code == "conflict"
    assert "start it again" in caught.value.message


async def test_only_the_settings_the_agent_lists_are_the_devices_to_change(
    harness: Harness,
) -> None:
    await harness.attach()
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options({"session_id": "sess-1", "permission_mode": "plan"})
    assert (caught.value.code, caught.value.message) == ("unsupported", "change it in the terminal")
    assert harness.terminal.typed == []
    # A rename is device-local and never went near the terminal.
    await harness.hub.set_options({"session_id": "sess-1", "title": "Reindex"})
    assert harness.hub.entry("sess-1").session.title == "Reindex"
    assert harness.terminal.typed == []


# ------------------------------------------------------------------ commands


async def test_a_shared_session_lists_compact_and_types_it(harness: Harness) -> None:
    entry = await harness.attach()
    listed = await harness.hub.commands({"session_id": "sess-1"})
    assert listed == {"commands": [COMPACT.to_dict()]}

    run = {"session_id": "sess-1", "name": "compact", "id": REQUEST}
    assert await harness.hub.command(run) == {}
    assert harness.terminal.typed == ["/compact\r"]
    echoes = harness.events("user_message")
    assert [(echo["block_id"], echo["text"], echo["source"]) for echo in echoes] == [
        (REQUEST, "/compact", "remote")
    ]
    # The row the CLI writes for it is ours, so it never becomes a second bubble.
    assert await harness.hub.shared.typed_command(entry, "/compact") is True
    assert await harness.hub.shared.typed_command(entry, "/compact") is False


async def test_a_command_the_person_typed_is_never_claimed(harness: Harness) -> None:
    entry = await harness.attach()
    assert await harness.hub.shared.typed_command(entry, "/clear") is False
    await harness.hub.command({"session_id": "sess-1", "name": "compact", "id": REQUEST})
    assert await harness.hub.shared.typed_command(entry, "/clear") is False


async def test_an_unknown_command_is_not_typed(harness: Harness) -> None:
    await harness.attach()
    with pytest.raises(RcError) as caught:
        await harness.hub.command({"session_id": "sess-1", "name": "review", "id": REQUEST})
    assert caught.value.code == "not_found"
    assert harness.terminal.typed == []


async def test_nothing_is_typed_while_a_turn_is_running(harness: Harness) -> None:
    entry = await harness.attach()
    state = entry.shared
    assert state is not None
    state.running = True
    with pytest.raises(RcError) as caught:
        await harness.hub.command({"session_id": "sess-1", "name": "compact", "id": REQUEST})
    assert (caught.value.code, caught.value.message) == ("conflict", typist.BUSY)
    assert harness.terminal.typed == []


async def test_the_transcript_reader_is_never_held_up_by_a_change_being_typed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model and effort in one request, and a message held meanwhile.

    Each change holds the session's lock until the reader confirms it, and the
    reader says after every read that the terminal is idle. Saying so must not
    wait for that lock, or the second confirmation never comes; the held
    message goes in once the change is done, never between its keys (A40).
    """
    monkeypatch.setattr(typist, "PROMPT_TIMEOUT", 0.1)
    monkeypatch.setattr(typist, "CONFIRM_TIMEOUT", 1.0)
    entry = await harness.attach()
    terminal = harness.terminal
    change = asyncio.create_task(
        harness.hub.set_options({"session_id": "sess-1", "model": "sonnet", "effort": "high"})
    )
    await _until(lambda: terminal.applied_model is not None)
    hold(entry.queue, pending_item("later", "req-h"), None)

    async def read(command: str, answer: str) -> None:
        # One pass of the mirror's tail loop over the rows the CLI wrote.
        await harness.confirm(entry, command, answer)
        await asyncio.wait_for(harness.hub.shared.tick(entry, running=False), timeout=0.5)

    await read("/model", "Set model to `Sonnet 4.5` for this session")
    await _until(lambda: terminal.applied_effort is not None)
    assert harness.attachment.injected == []
    await read("/effort", "Set effort level to high for this session")

    result = await asyncio.wait_for(change, timeout=2)
    assert (result["session"]["model"], result["session"]["effort"]) == ("sonnet", "high")
    await drained(entry)
    assert [text for _, text in harness.attachment.injected] == ["later"]


async def test_a_message_waits_behind_the_keystrokes(harness: Harness) -> None:
    """A20/A40: an injection must never land between two of the device's keys."""
    entry = await harness.attach()
    change = asyncio.create_task(harness.hub.set_options({"session_id": "sess-1", "model": "opus"}))
    await _until(lambda: harness.terminal.applied_model is not None)
    sending = asyncio.create_task(
        harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"})
    )
    await asyncio.sleep(0.05)
    assert not sending.done(), "the lock the script holds is what keeps them apart"

    await harness.confirm(entry, "/model", "Set model to `Opus` for this session")
    await asyncio.wait_for(change, timeout=2)
    assert await asyncio.wait_for(sending, timeout=2) == {"accepted": "sent"}
    assert [text for _, text in harness.attachment.injected] == ["go"]


# ---------------------------------------------------------------- stopping


async def running(harness: Harness, terminal: FakeTerminal | None = None) -> SessionEntry:
    """An attached session whose transcript says a turn is under way."""
    entry = await harness.attach(terminal)
    state = entry.shared
    assert state is not None
    state.running = True
    return entry


async def test_stop_presses_escape_while_a_turn_is_running(harness: Harness) -> None:
    """A42: the key the person would press, and nothing is waited for."""
    await running(harness)
    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert harness.terminal.typed == [typist.ESCAPE]


async def test_stop_does_not_wait_for_the_terminal_to_be_idle(harness: Harness) -> None:
    """The point of Stop is a terminal that is busy, half-typed line and all."""
    await running(harness, FakeTerminal(draft=7, idle_for=0.1))
    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert harness.terminal.typed == [typist.ESCAPE]


async def test_a_dialog_on_screen_is_answered_and_not_escaped(harness: Harness) -> None:
    entry = await running(harness)
    state = entry.shared
    assert state is not None

    await harness.hub.shared.permission_request(
        entry, {"request_id": "chan-1", "tool_name": "Bash", "input_preview": '{"command": "ls"}'}
    )
    with pytest.raises(RcError) as caught:
        await harness.hub.stop({"session_id": "sess-1"})
    assert (caught.value.code, caught.value.message) == ("conflict", typist.ANSWER_FIRST)

    state.approvals.clear()
    state.by_channel.clear()
    await harness.hub.shared.question(entry, FakeHookQuestion())
    with pytest.raises(RcError) as caught:
        await harness.hub.stop({"session_id": "sess-1"})
    assert (caught.value.code, caught.value.message) == ("conflict", typist.ANSWER_FIRST)
    assert harness.terminal.typed == []


async def test_stop_presses_escape_while_an_injection_is_still_in_flight(
    harness: Harness,
) -> None:
    """The message the device just injected is the CLI's turn already: the
    apps draw it as running, and Stop presses Escape rather than pass."""
    entry = await harness.attach()
    sent = await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go"})
    assert sent == {"accepted": "sent"}
    state = entry.shared
    assert state is not None
    assert (state.running, state.waiting) == (False, True)

    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert harness.terminal.typed == [typist.ESCAPE]


async def test_stop_on_a_session_with_no_turn_at_all_types_nothing(harness: Harness) -> None:
    entry = await harness.attach()
    state = entry.shared
    assert state is not None
    # Idle is neither of the two: no turn in the transcript, nothing in flight.
    assert (state.running, state.waiting) == (False, False)

    assert await harness.hub.stop({"session_id": "sess-1"}) == {}
    assert harness.terminal.typed == []


async def test_an_agent_that_cannot_be_stopped_still_says_so(tmp_path: Path) -> None:
    """Without the shim there is no terminal to type into, so no Stop either."""
    built = Harness(tmp_path, stops=False)
    try:
        await running(built)
        with pytest.raises(RcError) as caught:
            await built.hub.stop({"session_id": "sess-1"})
        assert (caught.value.code, caught.value.message) == (
            "unsupported",
            "stop it in the terminal",
        )
        assert built.terminal.typed == []
    finally:
        built.close()


# ------------------------------------------------------- what the agent says


def test_the_settings_keys_are_serialised_only_when_there_are_some() -> None:
    plain = AgentInfo(agent="codex", available=True, shared_settings=True)
    assert "shared_settings_keys" not in plain.to_dict()
    narrowed = AgentInfo(
        agent="claude", available=True, shared_settings=True, shared_settings_keys=["model"]
    )
    assert narrowed.to_dict()["shared_settings_keys"] == ["model"]


async def test_claude_reports_what_it_can_type_only_when_the_shim_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rc_client.agents.registry import DetectContext

    def ready(installed: bool) -> Callable[[], shim.ShimStatus]:
        def status() -> shim.ShimStatus:
            return shim.ShimStatus(
                installed=installed,
                path="/home/me/.rc-client/bin/claude",
                on_path=installed,
                real="/usr/local/bin/claude",
                mcp_config="/home/me/.rc-client/state/claude-mcp.json",
                settings="/home/me/.rc-client/state/claude-settings.json",
            )

        return status

    monkeypatch.setattr(shim, "status", ready(True))
    info = await claude_plugin.detect(DetectContext())
    assert (info.shared_settings, info.shared_settings_keys) == (True, ["model", "effort"])
    assert "commands" in info.capabilities
    # A42: Escape goes through the same terminal, so Stop rides on the shim too.
    assert (info.shared_interrupt, "interrupt" in info.capabilities) == (True, True)
    # A49: the channel the shim loads carries the paths of a message's files.
    assert (info.shared_attachments, "attachments" in info.capabilities) == (True, True)

    monkeypatch.setattr(shim, "status", ready(False))
    info = await claude_plugin.detect(DetectContext())
    assert (info.shared_settings, info.shared_settings_keys) == (False, None)
    assert info.shared_interrupt is False
    assert info.shared_attachments is False


async def test_the_sdk_session_runs_compact_as_a_prompt(tmp_path: Path) -> None:
    """A27 on a `remote` session: the CLI interprets its own command."""
    from rc_client.agents.claude.adapter import ClaudeRunner
    from rc_client.models import Session
    from rc_client.sessions.channel import SessionChannel

    registry = Registry(tmp_path / "state.sqlite3")
    sent: list[tuple[str, str | None]] = []

    async def publish(frame: dict[str, Any]) -> None:
        return None

    session = Session(session_id="sess-9", device_id="dev-1", agent="claude", cwd=str(tmp_path))
    runner = ClaudeRunner(
        SessionChannel(registry, session, publish), binary=None, cwd=str(tmp_path)
    )

    async def send(
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        sent.append((text, block_id))

    runner.send = send  # type: ignore[method-assign]
    try:
        assert await runner.commands() == [COMPACT]
        await runner.command("compact", None, REQUEST)
        assert sent == [("/compact", REQUEST)]
        with pytest.raises(RcError) as caught:
            await runner.command("review", None, REQUEST)
        assert caught.value.code == "not_found"
    finally:
        registry.close()


def test_every_model_and_effort_the_device_offers_has_somewhere_to_land() -> None:
    assert set(typist.MODEL_ROWS) == {choice.id for choice in claude_plugin.MODELS}
    assert list(typist.EFFORT_ORDER) == [choice.id for choice in claude_plugin.EFFORTS]
    # At least one step per level, so the walk starts from a known end.
    assert len(typist.EFFORT_ORDER) <= typist.EFFORT_STEPS


def test_the_shim_runs_the_cli_inside_the_proxy_and_falls_back_without_it() -> None:
    script = shim.render("/usr/local/bin/claude")
    assert f'-m {paths.pty_module()} -- "$REAL"' in script
    assert '[ -x "$RC_PYTHON" ]' in script
    # The fallback is the last word: a broken venv never costs anybody Claude.
    assert script.rstrip().endswith('exec "$REAL" "$@"')
