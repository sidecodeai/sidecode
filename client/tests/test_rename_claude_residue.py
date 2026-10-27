"""A55: a typed rename that nobody confirmed, and a terminal that fails while it is typed into."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from rc_client.errors import RcError
from rc_client.sessions import titles, typist
from rc_client.sessions.attach import TurnHook
from rc_client.sessions.ptys import PtyLink, TerminalGone, TerminalSilent, TerminalState
from tests.helpers import tick
from tests.rename_helpers import title_events
from tests.test_rename_claude_typed import _until, ask, claimed
from tests.test_shared_control import FakeAttachment
from tests.test_typist import FakeTerminal, Harness

LEDGER = "Ledger migration"
TYPED = f"{typist.RENAME_COMMAND} {LEDGER}{typist.ENTER}"
WARNING = "may still be in the prompt"


@pytest.fixture
async def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename nobody confirms gives up in a third of a second, not eight."""
    monkeypatch.setattr(typist, "CONFIRM_TIMEOUT", 0.3)
    monkeypatch.setattr(typist, "RENAME_POLL", 0.05)


class StuckTerminal(FakeTerminal):
    """A terminal where Enter submitted nothing: what was typed is still in the prompt box."""

    def _drawn(self) -> str:
        if self.typed and self.typed[-1].startswith(typist.RENAME_COMMAND):
            return f"╭{'-' * 30}╮\n│ > {self.typed[-1].strip()} │\n"
        return super()._drawn()


class Blind(FakeTerminal):
    """A terminal whose screen cannot be read."""

    async def screen(self) -> TerminalState:
        raise TerminalGone()


def unconfirmed(entry: Any) -> Any:
    """What the entry's attachment holds as unconfirmed, read afresh."""
    return entry.shared.unconfirmed if entry.shared is not None else None


def timeouts(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if WARNING in record.getMessage()]


async def times_out(harness: Harness) -> RcError:
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options(ask())
    return caught.value


async def refused(harness: Harness, **frame: Any) -> RcError:
    with pytest.raises(RcError) as caught:
        await harness.hub.set_options({"session_id": "sess-1", **frame})
    return caught.value


# ------------------------------------------------------------------ the text may still be there


async def test_a_rename_nobody_confirmed_may_have_left_its_text_and_the_terminal_is_marked(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = await harness.attach(StuckTerminal())

    error = await times_out(harness)

    assert (error.code, error.message) == ("timeout", typist.NOT_CONFIRMED)
    assert unconfirmed(entry) is not None
    # Nothing was sent to clean it up: no Escape, no second line.
    assert harness.terminal.typed == [TYPED]
    assert len(timeouts(caplog)) == 1


async def test_every_script_the_device_types_is_then_refused_and_nothing_more_is_typed(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = await harness.attach(StuckTerminal())
    await times_out(harness)

    for frame in ({"model": "sonnet"}, {"effort": "high"}, {"title": "Another name"}):
        error = await refused(harness, **frame)
        assert (error.code, error.message) == ("conflict", typist.UNCONFIRMED), frame
    with pytest.raises(RcError) as compact:
        await harness.hub.command({"session_id": "sess-1", "name": "compact", "id": "req-1"})
    assert (compact.value.code, compact.value.message) == ("conflict", typist.UNCONFIRMED)

    assert harness.terminal.typed == [TYPED]
    assert entry.session.title == ""
    assert not titles.pinned(harness.registry, "sess-1")
    assert len(timeouts(caplog)) == 1, "said once, however often the terminal is refused"
    assert not entry.lock.locked()


async def test_a_rename_that_left_nothing_on_the_screen_leaves_the_terminal_alone(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = await harness.attach()

    error = await times_out(harness)

    assert (error.code, error.message) == ("timeout", typist.NOT_CONFIRMED)
    assert unconfirmed(entry) is None
    assert timeouts(caplog) == []
    # The next rename is typed as any other.
    second = asyncio.create_task(harness.hub.set_options(ask("Another name")))
    await _until(lambda: len(harness.terminal.typed) == 2)
    await harness.hub.shared.title_seen(entry, "Another name")
    assert (await asyncio.wait_for(second, timeout=2))["session"]["title"] == "Another name"


async def test_a_screen_that_cannot_be_read_is_taken_for_one_that_holds_the_text(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = await harness.attach(Blind())

    await times_out(harness)

    assert unconfirmed(entry) is not None
    assert harness.terminal.typed == [TYPED]
    assert len(timeouts(caplog)) == 1
    error = await refused(harness, title="Another name")
    assert (error.code, error.message) == ("conflict", typist.UNCONFIRMED)


@pytest.mark.parametrize("name", [LEDGER, "x" * 60, "a b c d e f g h i j k l m n o p q r s t"])
def test_the_text_is_looked_for_by_its_first_words_wherever_the_box_wraps_it(name: str) -> None:
    box = f"│ > /rename {name[:30]}\n│   {name[30:]} │"
    assert typist.may_hold(box, name)
    assert typist.may_hold(f"> /rename {name}", name)
    assert not typist.may_hold("> \n", name)
    assert not typist.may_hold(f"> /model {name}", name)


# ------------------------------------------------------------------ and then it clears


async def test_a_key_the_person_presses_clears_it_and_the_gate_goes_on_to_its_own_checks(
    harness: Harness,
) -> None:
    terminal = StuckTerminal()
    entry = await harness.attach(terminal)
    await times_out(harness)
    assert entry.shared is not None

    # Nobody has touched the keyboard since, however long the terminal has been quiet.
    assert (await refused(harness, title="Another name")).message == typist.UNCONFIRMED
    assert unconfirmed(entry) is not None

    # The person pressed a key a moment ago: the mark goes, and the keyboard is busy as ever.
    terminal.idle_for = 0.1
    assert (await refused(harness, title="Another name")).message == typist.BUSY
    assert unconfirmed(entry) is None

    # Still for two seconds, the device may type again.
    terminal.idle_for = 5.0
    terminal.draft = 0
    again = asyncio.create_task(harness.hub.set_options(ask("Another name")))
    await _until(lambda: len(terminal.typed) == 2)
    assert terminal.typed[-1] == f"{typist.RENAME_COMMAND} Another name{typist.ENTER}"
    await harness.hub.shared.title_seen(entry, "Another name")
    assert (await asyncio.wait_for(again, timeout=2))["session"]["title"] == "Another name"


async def test_a_turn_that_starts_clears_it(harness: Harness) -> None:
    entry = await harness.attach(StuckTerminal())
    await times_out(harness)
    assert unconfirmed(entry) is not None

    await tick(harness.hub, entry, running=True)
    assert unconfirmed(entry) is None
    await tick(harness.hub, entry, running=False)

    assert claimed(entry) is None
    second = asyncio.create_task(harness.hub.set_options(ask("Another name")))
    await _until(lambda: len(harness.terminal.typed) == 2)
    await harness.hub.shared.title_seen(entry, "Another name")
    await asyncio.wait_for(second, timeout=2)


async def test_a_prompt_that_reaches_the_cli_clears_it(harness: Harness) -> None:
    entry = await harness.attach(StuckTerminal())
    await times_out(harness)
    assert unconfirmed(entry) is not None

    await harness.hub.attach_turn(TurnHook("sess-1", "prompt", prompt_kind="person"))

    assert unconfirmed(entry) is None


async def test_the_end_of_the_attachment_clears_it(harness: Harness) -> None:
    entry = await harness.attach(StuckTerminal())
    await times_out(harness)
    assert unconfirmed(entry) is not None

    await harness.hub.shared.closed(entry, harness.attachment, "none")
    assert entry.shared is None
    await harness.hub.attach_registered(FakeAttachment())

    assert unconfirmed(entry) is None


async def test_stop_is_not_a_script_the_mark_refuses(harness: Harness) -> None:
    """Stop stays a safety control: an Escape for a running turn, whatever is in the box."""
    terminal = StuckTerminal()
    entry = await harness.attach(terminal)
    await times_out(harness)
    assert entry.shared is not None
    entry.shared.running = True

    await harness.hub.stop({"session_id": "sess-1"})

    assert terminal.typed[-1] == typist.ESCAPE


# ------------------------------------------------------------------ a terminal that fails


class FailsToAnswer(FakeTerminal):
    """A terminal whose proxy fails the request named, with the error named."""

    def __init__(self, request: str, error: RcError, *, after: bool = False) -> None:
        super().__init__()
        self.request, self.error, self.after = request, error, after

    async def state(self) -> TerminalState:
        if self.request == "state":
            raise self.error
        return await super().state()

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        if self.request != "keys":
            return await super().keys(data, clear)
        if self.after:
            await super().keys(data, clear)
        raise self.error


async def test_a_terminal_that_is_gone_before_anything_is_typed_is_unsupported(
    harness: Harness,
) -> None:
    entry = await harness.attach(FailsToAnswer("state", TerminalGone()))

    error = await times_out(harness)

    assert (error.code, error.message) == ("unsupported", typist.NOT_TYPEABLE)
    assert harness.terminal.typed == []
    assert claimed(entry) is None and unconfirmed(entry) is None


async def test_a_proxy_that_does_not_answer_before_anything_is_typed_is_a_timeout(
    harness: Harness,
) -> None:
    entry = await harness.attach(FailsToAnswer("state", TerminalSilent()))

    error = await times_out(harness)

    assert (error.code, error.message) == ("timeout", "the terminal did not answer in time")
    assert harness.terminal.typed == []
    assert claimed(entry) is None and unconfirmed(entry) is None


async def test_keys_that_never_left_are_unsupported_and_are_not_looked_for(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = await harness.attach(FailsToAnswer("keys", TerminalGone()))

    error = await times_out(harness)

    assert (error.code, error.message) == ("unsupported", typist.NOT_TYPEABLE)
    assert harness.terminal.typed == []
    assert claimed(entry) is None and unconfirmed(entry) is None
    assert timeouts(caplog) == []
    assert title_events(harness.frames) == []


class SilentAfterKeys(StuckTerminal):
    """The proxy took the keys and then went quiet."""

    async def keys(self, data: str, clear: bool = False) -> TerminalState:
        await super().keys(data, clear)
        raise TerminalSilent()


async def test_keys_a_silent_proxy_may_have_taken_are_a_timeout_and_the_screen_is_looked_at(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    entry = await harness.attach(SilentAfterKeys())

    error = await times_out(harness)

    assert (error.code, error.message) == ("timeout", typist.NOT_CONFIRMED)
    assert harness.terminal.typed == [TYPED]
    assert claimed(entry) is None
    assert unconfirmed(entry) is not None
    assert len(timeouts(caplog)) == 1


async def test_any_other_failure_of_the_keys_is_a_timeout_too(harness: Harness) -> None:
    unknown = RcError("conflict", "this terminal is no longer attached")
    entry = await harness.attach(FailsToAnswer("keys", unknown, after=True))

    error = await times_out(harness)

    assert (error.code, error.message) == ("timeout", typist.NOT_CONFIRMED)
    assert claimed(entry) is None


async def test_a_busy_terminal_is_still_a_conflict(harness: Harness) -> None:
    await harness.attach(FakeTerminal(draft=2))

    error = await times_out(harness)

    assert (error.code, error.message) == ("conflict", typist.BUSY)


# ------------------------------------------------------------------ the link itself


class _Quiet:
    """A stream a request can be written to and nothing ever answers."""

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Broken(_Quiet):
    async def drain(self) -> None:
        raise ConnectionResetError


async def test_a_link_that_was_never_there_says_so_and_one_that_is_silent_says_that(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = PtyLink(1, cast(asyncio.StreamWriter, _Quiet()))
    monkeypatch.setattr("rc_client.sessions.ptys.REQUEST_TIMEOUT", 0.05)

    with pytest.raises(TerminalSilent) as silent:
        await link.state()
    assert (silent.value.code, silent.value.message) == (
        "conflict",
        "the terminal did not answer in time",
    )

    link.detach()
    with pytest.raises(TerminalGone) as gone:
        await link.keys("x")
    assert (gone.value.code, gone.value.message) == (
        "conflict",
        "this terminal is no longer attached",
    )


async def test_a_link_that_drops_after_a_request_went_out_does_not_say_it_never_left() -> None:
    link = PtyLink(1, cast(asyncio.StreamWriter, _Quiet()))
    request = asyncio.create_task(link.keys("x"))
    await asyncio.sleep(0.02)

    link.detach()

    with pytest.raises(TerminalSilent) as silent:
        await request
    assert silent.value.message == "this terminal is no longer attached"


async def test_a_stream_that_fails_to_take_the_request_is_a_terminal_that_is_gone() -> None:
    link = PtyLink(1, cast(asyncio.StreamWriter, _Broken()))

    with pytest.raises(TerminalGone):
        await link.state()

    assert not link.alive
