"""A prompt a subagent raised, answered in the terminal, no longer sits on the apps.

The CLI relays a subagent's permission prompt to the device as it does the main
thread's, and tells it nothing when the person answers in the terminal. For a
main-thread tool the transcript's own result row says so; a subagent's result
is in its own transcript, which is read, only while a prompt waits, to find the
call the prompt is about and see it end. A prompt that cannot be matched with a
call for certain is left as it is: a card closed too early hides a prompt that
is still waiting.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import subagents
from rc_client.agents.claude.calls import read_calls as real_read_calls
from rc_client.agents.claude.plans import PLAN_TOOL
from rc_client.agents.claude.questions import QUESTION_TOOL
from rc_client.sessions.hub import SessionEntry
from rc_client.sessions.shared import CALL_LOOK

from .test_claude_calls import BUILD, ended, made, preview
from .test_claude_subagents import MirrorHarness, assistant, user_row
from .test_previews import DOT, text
from .test_shared_control import FakeHookQuestion

TEST = {"command": "pytest -q tests/test_refresh.py", "description": "Run the refresh tests"}


@pytest.fixture
async def mirrored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    built = MirrorHarness(tmp_path, monkeypatch)
    yield built
    await built.hub.close()
    built.close()


class Reader:
    """Stands in for the mirror's side of the transcript reader, and remembers what it was asked."""

    def __init__(self) -> None:
        self.looks: list[tuple[str, float]] = []

    def read_soon(self) -> None:
        return None

    def settle_soon(self, session_id: str, delay: float) -> None:
        self.looks.append((session_id, delay))


def shown(entry: SessionEntry) -> str:
    """The state the apps are told, read afresh each time: it moves under the test's feet."""
    return entry.session.state


def approvals(mirrored: MirrorHarness) -> list[dict[str, Any]]:
    return mirrored.events("approval")


def settled(mirrored: MirrorHarness) -> list[dict[str, Any]]:
    """The last word of each approval block the apps were sent."""
    last: dict[str, dict[str, Any]] = {}
    for event in approvals(mirrored):
        last[event["request_id"]] = event
    return list(last.values())


async def ask(
    mirrored: MirrorHarness, tool_input: dict[str, Any], request_id: str, tool: str = "Bash"
) -> None:
    """The CLI relays a prompt for this tool call, as it does for every tool."""
    await mirrored.hub.attach_permission_request(
        mirrored.attachment,
        {
            "request_id": request_id,
            "tool_name": tool,
            "description": str(tool_input.get("description") or ""),
            "input_preview": preview(tool_input),
        },
    )


async def running_turn(mirrored: MirrorHarness) -> SessionEntry:
    """An attached session whose turn is under way, and one subagent writing its transcript."""
    entry: SessionEntry = await mirrored.attach()
    mirrored.write([user_row("go", "u1")])
    await mirrored.mirror.tail_once()
    return entry


def pending(entry: SessionEntry) -> dict[str, Any]:
    assert entry.shared is not None
    return dict(entry.shared.approvals)


def bound(approval: Any) -> str | None:
    """The call a prompt was paired with, read afresh each time."""
    call = approval.call
    return call if isinstance(call, str) else None


def asking(entry: SessionEntry) -> Any:
    """The question the CLI has open, read afresh each time."""
    assert entry.shared is not None
    return entry.shared.question


# ---------------------------------------------------------------- the reported case


async def test_a_subagents_prompt_answered_in_the_terminal_closes_its_card(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await mirrored.mirror.tail_once()
    assert shown(entry) == "running"

    await ask(mirrored, BUILD, "rq1")
    assert shown(entry) == "needs_approval"
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call == "t1", "the prompt is paired with the call it waits on"
    assert shown(entry) == "needs_approval", "and stays while that call has no result"

    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    [last] = settled(mirrored)
    assert last["status"] == "resolved"
    assert last["decision"] == {"option_id": "allow", "by": "terminal"}
    assert shown(entry) == "running", "back to the subagent's work"


async def test_a_call_that_failed_was_refused_and_says_so(mirrored: MirrorHarness) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    mirrored.subagent("agent-a1", [ended("t1", failed=True)])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "deny", "by": "terminal"}


async def test_the_call_is_looked_for_as_soon_as_the_prompt_is_raised(
    mirrored: MirrorHarness,
) -> None:
    """Not at the next two-second pass: a person at the terminal is quick."""
    entry = await running_turn(mirrored)
    reader = Reader()
    mirrored.hub.shared.reader = reader
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await ask(mirrored, BUILD, "rq1")
    assert reader.looks == [("sess-1", CALL_LOOK)]
    assert pending(entry)


async def test_a_prompt_answered_in_an_app_needs_no_call_to_end(mirrored: MirrorHarness) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [request_id] = pending(entry)
    await mirrored.hub.approve(
        {"session_id": "sess-1", "request_id": request_id, "option_id": "allow"}
    )
    assert pending(entry) == {}
    resolved = settled(mirrored)[-1]["decision"]
    assert resolved == {"option_id": "allow", "by": "remote"}
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert settled(mirrored)[-1]["decision"] == resolved, "nothing is said twice"


# ------------------------------------------------------------- what is left alone


async def test_a_prompt_that_fits_no_call_is_not_closed_by_a_result_of_the_same_tool(
    mirrored: MirrorHarness,
) -> None:
    """The failure that matters: another subagent's `Bash` ends while this prompt still waits."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent(
        "agent-a1", [made("t1", "Bash", TEST, at=now), made("t9", "Bash", BUILD, at=now)]
    )
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call == "t9", "the prompt is about the build, not the test run"
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert list(pending(entry).values()) == [approval], "the test run ending says nothing of it"
    assert shown(entry) == "needs_approval"


@pytest.mark.parametrize(
    "showing",
    [
        {"command": "rm -rf build && make all"},
        {
            "command": "something else entirely, a long one",
            "description": "Clean and rebuild everything",
        },
        {},
    ],
    ids=["a field is missing", "the command differs", "nothing is shown"],
)
async def test_a_preview_that_does_not_show_the_call_pairs_nothing(
    mirrored: MirrorHarness, showing: dict[str, Any]
) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await ask(mirrored, showing, "rq1")
    await mirrored.mirror.tail_once()
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call is None
    assert shown(entry) == "needs_approval"


async def test_two_calls_that_fit_one_prompt_pair_nothing(mirrored: MirrorHarness) -> None:
    """Either may be only running; whichever ends first would close a card still waiting."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    mirrored.subagent("agent-a2", [made("t2", "Bash", BUILD, at=now)])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert bound(approval) is None

    mirrored.subagent("agent-a2", [ended("t2")])
    await mirrored.mirror.tail_once()
    assert list(pending(entry).values()) == [approval], "one of them ended, and the card stays"
    assert bound(approval) == "t1", "the other is the only call left it can be"
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}


async def test_prompts_that_look_alike_pair_with_nothing_and_both_cards_stay(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    mirrored.subagent("agent-a2", [made("t2", "Bash", BUILD, at=now + 0.1)])
    await ask(mirrored, BUILD, "rq1")
    await ask(mirrored, BUILD, "rq2")
    await mirrored.mirror.tail_once()
    assert [bound(a) for a in pending(entry).values()] == [None, None]

    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert len(pending(entry)) == 2, "one call ended, and which card was its cannot be told"
    assert shown(entry) == "needs_approval"


async def test_a_card_the_phone_answered_leaves_the_one_that_still_waits(
    mirrored: MirrorHarness,
) -> None:
    """Raised in the opposite order to the calls: a guessed pairing would close the wrong card."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    mirrored.subagent("agent-a2", [made("t2", "Bash", BUILD, at=now + 0.2)])
    await ask(mirrored, BUILD, "r-first")  # really t2's
    await ask(mirrored, BUILD, "r-second")  # really t1's
    await mirrored.mirror.tail_once()
    second = next(a for a in pending(entry).values() if a.channel_id == "r-second")
    await mirrored.hub.approve(
        {"session_id": "sess-1", "request_id": second.request_id, "option_id": "allow"}
    )
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    [left] = pending(entry).values()
    assert left.channel_id == "r-first", "t2's prompt still waits, and so does its card"
    assert shown(entry) == "needs_approval"


async def test_a_prompt_the_main_thread_raised_is_not_paired_with_a_subagents_identical_call(
    mirrored: MirrorHarness,
) -> None:
    """Its own call is open in its transcript, and the subagent's command only runs."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    mirrored.write([made("m1", "Bash", BUILD, at=now)])
    await ask(mirrored, BUILD, "rq-main")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert bound(approval) is None

    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert list(pending(entry).values()) == [approval], "the subagent's command ending says nothing"
    assert shown(entry) == "needs_approval"

    mirrored.write([ended("m1")])  # the person answered in the terminal, and its own result says so
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "allow", "by": "terminal"}


async def test_a_main_thread_call_that_shows_up_after_the_pairing_undoes_it(
    mirrored: MirrorHarness,
) -> None:
    """Its row was not written yet at the first look, and the subagent's call ends first."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    await ask(mirrored, BUILD, "rq-main")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert bound(approval) == "t1"

    mirrored.write([made("m1", "Bash", BUILD, at=now)])
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert list(pending(entry).values()) == [approval]
    assert bound(approval) is None
    assert shown(entry) == "needs_approval"


async def test_a_main_thread_call_that_shows_up_later_undoes_the_pairing_at_once(
    mirrored: MirrorHarness,
) -> None:
    """Paired while the subagent's call ran; the main thread's own call turns out to be open too."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    await ask(mirrored, BUILD, "rq-main")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert bound(approval) == "t1"

    mirrored.write([made("m1", "Bash", BUILD, at=now)])
    await mirrored.mirror.tail_once()
    assert bound(approval) is None, "and t1 has not ended"

    mirrored.write([ended("m1")])  # the person answered in the terminal
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "allow", "by": "terminal"}


async def test_a_later_identical_call_keeps_the_card_when_the_paired_one_ends(
    mirrored: MirrorHarness,
) -> None:
    """The paired call may have been only running, and the later one the prompt's own."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert bound(approval) == "t1"

    mirrored.subagent("agent-a2", [made("t2", "Bash", BUILD, at=now + 0.5)])
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert list(pending(entry).values()) == [approval], "t1 ending says nothing of it now"
    assert bound(approval) == "t2", "the pairing was a guess, and the one call it can be is t2"
    assert shown(entry) == "needs_approval"


async def test_a_call_made_long_before_the_prompt_is_not_its_call(mirrored: MirrorHarness) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time() - 3600)])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call is None


async def test_the_main_threads_own_result_does_not_close_a_subagents_prompt(
    mirrored: MirrorHarness,
) -> None:
    """The main transcript closes a prompt by the tool's name; this one is a subagent's."""
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    mirrored.write([made("m1", "Bash", TEST, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call == "t1"


async def test_a_prompt_for_a_main_thread_tool_is_still_closed_by_the_main_transcript(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    await ask(mirrored, BUILD, "rq1")
    mirrored.write([made("m1", "Bash", BUILD, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "allow", "by": "terminal"}


async def test_with_no_subagent_about_any_result_of_the_tool_closes_its_prompt(
    mirrored: MirrorHarness,
) -> None:
    """Nobody else could have raised it, so the name says enough."""
    entry = await running_turn(mirrored)
    await ask(mirrored, BUILD, "rq1")
    mirrored.write([made("m1", "Bash", TEST, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}


async def test_the_main_threads_result_read_with_the_pairing_closes_its_prompt(
    mirrored: MirrorHarness,
) -> None:
    """Answered before the first look: its rows are read in the pass that would pair it."""
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=now)])
    await ask(mirrored, BUILD, "rq-main")
    mirrored.write([made("m1", "Bash", BUILD, at=now), ended("m1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "allow", "by": "terminal"}

    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert len(approvals(mirrored)) == 2, "and nothing is said of it again"


async def test_a_main_prompt_with_a_typographic_quote_is_closed_by_its_own_result(
    mirrored: MirrorHarness,
) -> None:
    """The preview writes the quote as a middle dot, and the input is looked for as written."""
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [assistant("tool_use")])  # a subagent at work
    await mirrored.mirror.tail_once()
    edit = {
        "file_path": "/repo/NOTES.md",
        "old_string": text("don", 0x2019, "t panic"),
        "new_string": "do not panic",
    }
    shown_as = preview(edit).replace(chr(0x2019), chr(DOT))
    await mirrored.hub.attach_permission_request(
        mirrored.attachment,
        {"request_id": "rq1", "tool_name": "Edit", "description": "", "input_preview": shown_as},
    )
    mirrored.write([made("m1", "Edit", edit, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "allow", "by": "terminal"}


async def test_a_short_main_command_has_no_text_to_compare_and_closes_when_it_is_alone(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [assistant("tool_use")])
    await mirrored.mirror.tail_once()
    await ask(mirrored, {"command": "pwd"}, "rq1")
    mirrored.write([made("m1", "Bash", {"command": "pwd"}, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}

    await ask(mirrored, {"command": "pwd"}, "rq2")
    await ask(mirrored, {"command": "ls"}, "rq3")
    mirrored.write([made("m2", "Bash", {"command": "pwd"}, at=time.time()), ended("m2")])
    await mirrored.mirror.tail_once()
    assert len(pending(entry)) == 2, "which of two was answered cannot be told"


async def test_prompts_that_look_alike_are_closed_by_no_result_of_the_main_thread(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [assistant("tool_use")])
    await mirrored.mirror.tail_once()
    await ask(mirrored, BUILD, "rq1")
    await ask(mirrored, BUILD, "rq2")
    mirrored.write([made("m1", "Bash", BUILD, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert len(pending(entry)) == 2
    assert shown(entry) == "needs_approval"


async def test_a_plan_the_main_thread_asked_is_answered_by_its_own_result_with_subagents_about(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [assistant("tool_use")])
    await mirrored.mirror.tail_once()
    hook = FakeHookQuestion()
    hook.tool, hook.input = PLAN_TOOL, {"plan": "# Plan\n- one"}
    await mirrored.hub.attach_question(hook)
    assert len(pending(entry)) == 1
    mirrored.write([made("m1", PLAN_TOOL, {"plan": "# Plan\n- one"}, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "elsewhere", "by": "terminal"}


async def test_with_subagents_about_only_the_result_that_shows_the_prompts_input_closes_it(
    mirrored: MirrorHarness,
) -> None:
    """Another command's result says nothing of a prompt a subagent may have raised."""
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [assistant("tool_use")])  # at work, with no call to see
    await mirrored.mirror.tail_once()
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert bound(approval) is None

    mirrored.write([made("m1", "Bash", TEST, at=time.time()), ended("m1")])
    await mirrored.mirror.tail_once()
    assert list(pending(entry).values()) == [approval], "an unrelated command of the main thread"
    assert shown(entry) == "needs_approval"

    mirrored.write([made("m2", "Bash", BUILD, at=time.time()), ended("m2")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}, "its own command, which the person allowed in the terminal"
    assert settled(mirrored)[-1]["decision"] == {"option_id": "allow", "by": "terminal"}


async def test_the_subagents_are_read_only_while_a_prompt_waits(
    mirrored: MirrorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = await running_turn(mirrored)
    reads: list[float] = []
    real = subagents.SubagentWatch.calls

    def counted(self: subagents.SubagentWatch, since: float) -> Any:
        reads.append(since)
        return real(self, since)

    monkeypatch.setattr(subagents.SubagentWatch, "calls", counted)
    mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    await mirrored.mirror.tail_once()
    assert reads == [], "nothing waits, nothing is read"
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    assert len(reads) == 1
    mirrored.subagent("agent-a1", [ended("t1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    await mirrored.mirror.tail_once()
    assert len(reads) == 2, "and no more once the prompt is gone"


async def test_a_transcript_that_has_not_grown_is_not_read_again(
    mirrored: MirrorHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await running_turn(mirrored)
    path = mirrored.subagent("agent-a1", [made("t1", "Bash", BUILD, at=time.time())])
    watch = subagents.SubagentWatch(directory=mirrored.subagents)
    reads: list[str] = []

    def counted(path: Any, *args: Any) -> Any:
        reads.append(str(path))
        return real_read_calls(path, *args)

    monkeypatch.setattr("rc_client.agents.claude.subagents.read_calls", counted)
    since = time.time() - 600
    assert "t1" in watch.calls(since)
    assert "t1" in watch.calls(since)
    assert reads == [str(path)]
    mirrored.subagent("agent-a1", [ended("t1")])
    assert watch.calls(since)["t1"].closed
    assert len(reads) == 2


# --------------------------------------------------------------------- where they live


async def test_a_workflows_subagent_files_its_transcript_a_level_deeper(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    nested = mirrored.subagents / "workflows" / "run-1"
    nested.mkdir(parents=True)
    path = nested / "agent-w1.jsonl"
    rows = [made("w1", "Bash", BUILD, at=time.time())]
    import json

    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call == "w1"
    path.write_text(path.read_text() + json.dumps(ended("w1")) + "\n", encoding="utf-8")
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}


# ------------------------------------------------------------------ plans and questions


async def test_a_question_a_subagent_asked_ends_with_the_answer_its_result_row_records(
    mirrored: MirrorHarness,
) -> None:
    """A fork has the main conversation's whole tool pool, `AskUserQuestion` among it."""
    entry = await running_turn(mirrored)
    call_input = {"questions": [{"question": "How should the refresh window treat skew?"}]}
    mirrored.subagent("agent-a1", [made("q1", QUESTION_TOOL, call_input, at=time.time())])
    hook = FakeHookQuestion()
    await mirrored.hub.attach_question(hook)
    assert shown(entry) == "needs_input"
    await mirrored.mirror.tail_once()
    assert asking(entry).call == "q1"

    answers = {"How should the refresh window treat skew?": "Go monotonic"}
    mirrored.subagent("agent-a1", [ended("q1", toolUseResult={"answers": answers})])
    await mirrored.mirror.tail_once()
    assert asking(entry) is None
    resolved = mirrored.events("question")[-1]
    assert resolved["status"] == "resolved" and resolved["by"] == "terminal"
    assert resolved["answers"] == {"q0": ["o1"]}
    assert hook.replies == [None], "the hook is told to stand down"
    assert shown(entry) == "running"


async def test_the_main_threads_own_answer_does_not_close_a_subagents_question(
    mirrored: MirrorHarness,
) -> None:
    """The main transcript closes a question by its tool; this one is paired with a subagent's."""
    entry = await running_turn(mirrored)
    call_input = {"questions": [{"question": "How should the refresh window treat skew?"}]}
    mirrored.subagent("agent-a1", [made("q1", QUESTION_TOOL, call_input, at=time.time())])
    await mirrored.hub.attach_question(FakeHookQuestion())
    await mirrored.mirror.tail_once()
    assert asking(entry).call == "q1"

    answers = {"Some other question the main thread asked": "Yes"}
    mirrored.write(
        [
            made("m1", QUESTION_TOOL, {"questions": []}, at=time.time()),
            ended("m1", toolUseResult={"answers": answers}),
        ]
    )
    await mirrored.mirror.tail_once()
    assert asking(entry) is not None and asking(entry).call == "q1"
    assert shown(entry) == "needs_input"


async def test_a_question_whose_call_ends_while_another_could_be_it_is_not_answered(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("q1", QUESTION_TOOL, {"questions": []}, at=now)])
    await mirrored.hub.attach_question(FakeHookQuestion())
    await mirrored.mirror.tail_once()
    assert asking(entry).call == "q1"

    mirrored.subagent("agent-a2", [made("q2", QUESTION_TOOL, {"questions": []}, at=now + 0.5)])
    mirrored.subagent("agent-a1", [ended("q1", toolUseResult={"answers": {"Which?": "This"}})])
    await mirrored.mirror.tail_once()
    assert asking(entry) is not None, "q1 may have been only running, and q2 the question's own"
    assert asking(entry).call == "q2"
    assert shown(entry) == "needs_input"


async def test_two_open_questions_pair_with_nothing(mirrored: MirrorHarness) -> None:
    entry = await running_turn(mirrored)
    now = time.time()
    mirrored.subagent("agent-a1", [made("q1", QUESTION_TOOL, {"questions": []}, at=now)])
    mirrored.subagent("agent-a2", [made("q2", QUESTION_TOOL, {"questions": []}, at=now)])
    await mirrored.hub.attach_question(FakeHookQuestion())
    await mirrored.mirror.tail_once()
    assert asking(entry).call is None


async def test_a_plan_a_subagent_asked_approval_of_ends_as_the_terminals(
    mirrored: MirrorHarness,
) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent(
        "agent-a1", [made("p1", PLAN_TOOL, {"plan": "# Plan\n- one"}, at=time.time())]
    )
    hook = FakeHookQuestion()
    hook.tool, hook.input = PLAN_TOOL, {"plan": "# Plan\n- one"}
    await mirrored.hub.attach_question(hook)
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call == "p1"
    mirrored.subagent("agent-a1", [ended("p1")])
    await mirrored.mirror.tail_once()
    assert pending(entry) == {}
    assert settled(mirrored)[-1]["decision"] == {"option_id": "elsewhere", "by": "terminal"}


async def test_an_assistant_row_with_no_call_in_it_changes_nothing(mirrored: MirrorHarness) -> None:
    entry = await running_turn(mirrored)
    mirrored.subagent("agent-a1", [assistant("end_turn")])
    await ask(mirrored, BUILD, "rq1")
    await mirrored.mirror.tail_once()
    [approval] = pending(entry).values()
    assert approval.call is None
