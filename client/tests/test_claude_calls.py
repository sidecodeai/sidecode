"""The tool calls a transcript shows, and when a prompt can be matched with one for certain."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude.calls import (
    LOOKAHEAD,
    LOOKBACK,
    Call,
    calls_in,
    fits,
    open_calls,
    read_calls,
)
from rc_client.sessions.prompt_calls import (
    about_input,
    answered_by_result,
    bind_approvals,
    bind_question,
    contested_approval,
    contested_question,
)

BUILD = {"command": "rm -rf build && make all", "description": "Clean and rebuild everything"}


def stamp(at: float | None) -> dict[str, str]:
    if at is None:
        return {}
    return {"timestamp": datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")}


def made(
    call_id: str, name: str, tool_input: dict[str, Any], at: float | None = None
) -> dict[str, Any]:
    """The row Claude Code files for one `tool_use` block of an assistant message."""
    block = {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
    message = {"id": f"msg-{call_id}", "content": [block], "stop_reason": "tool_use"}
    return {"type": "assistant", "uuid": f"row-{call_id}", "message": message, **stamp(at)}


def ended(call_id: str, failed: bool = False, **extra: Any) -> dict[str, Any]:
    """The row that carries the result of the call."""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}
    if failed:
        block["is_error"] = True
    message = {"role": "user", "content": [block]}
    return {"type": "user", "uuid": f"result-{call_id}", "message": message, **extra}


def preview(tool_input: dict[str, Any]) -> str:
    """What the CLI's permission relay shows of an input: each value as JSON, spaces folded."""
    shown = ", ".join(
        f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in tool_input.items()
    )
    return "{ " + shown + " }"


# ---------------------------------------------------------------- reading rows


def test_a_call_is_open_until_its_result_row_and_remembers_how_it_ended() -> None:
    rows = [made("t1", "Bash", BUILD), made("t2", "Write", {"file_path": "/repo/out/report.txt"})]
    found = calls_in([*rows, ended("t1"), ended("t2", failed=True)])
    assert (found["t1"].closed, found["t1"].failed) == (True, False)
    assert (found["t2"].closed, found["t2"].failed) == (True, True)
    assert not calls_in(rows)["t1"].closed


def test_a_result_for_a_call_not_in_the_rows_is_nobodys() -> None:
    assert calls_in([ended("elsewhere")]) == {}


def test_a_row_carries_its_own_time_when_it_has_one() -> None:
    now = time.time()
    [call] = calls_in([made("t1", "Bash", BUILD, at=now)]).values()
    assert call.at is not None and abs(call.at - now) < 1
    [bare] = calls_in([made("t2", "Bash", BUILD)]).values()
    assert bare.at is None
    odd = {**made("t3", "Bash", BUILD), "timestamp": "yesterday"}
    assert next(iter(calls_in([odd]).values())).at is None


def test_an_question_answer_is_kept_with_the_call_that_asked() -> None:
    asked = made("q1", "AskUserQuestion", {"questions": [{"question": "Which way?"}]})
    result = ended("q1", toolUseResult={"answers": {"Which way?": "Left"}})
    [call] = calls_in([asked, result]).values()
    assert call.closed and call.answers == {"Which way?": "Left"}
    plain = calls_in([made("t1", "Bash", BUILD), ended("t1")])["t1"]
    assert plain.answers is None


@pytest.mark.parametrize(
    "row",
    [
        {"type": "assistant"},
        {"type": "assistant", "message": "not an object"},
        {"type": "assistant", "message": {"content": "text"}},
        {"type": "assistant", "message": {"content": ["x", None, {"type": "tool_use"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "progress", "data": {}},
    ],
)
def test_rows_that_are_not_calls_are_read_as_none(row: dict[str, Any]) -> None:
    assert calls_in([row]) == {}


def test_a_transcripts_tail_is_read_and_its_first_fragment_is_not(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.jsonl"
    rows = [made(f"old{n}", "Bash", {"command": f"echo number {n} of many"}) for n in range(40)]
    rows += [made("now", "Bash", BUILD)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    found = read_calls(path, window=2000)
    assert "now" in found and not found["now"].closed
    assert "old0" not in found
    assert read_calls(tmp_path / "gone.jsonl") == {}


def test_a_tail_that_begins_inside_a_multibyte_character_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "agent-a1.jsonl"
    rows = [made("t1", "Bash", {"command": "你" * 3000}), made("t2", "Bash", BUILD)]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")
    for window in range(1000, 1004):
        assert "t2" in read_calls(path, window=window)


# ------------------------------------------------------------- matching a preview


def test_a_preview_shows_the_text_of_its_call() -> None:
    [call] = calls_in([made("t1", "Bash", BUILD)]).values()
    assert fits(preview(BUILD), call)
    assert fits(preview(BUILD).replace(", ", ",    "), call), "runs of spaces are folded"
    assert not fits(preview({**BUILD, "command": "rm -rf build && make test"}), call)
    assert not fits("", call)


def test_a_value_that_json_escapes_is_matched_as_the_preview_writes_it() -> None:
    tool_input = {"command": 'printf "%s\\n" "first line"\nprintf "second line"'}
    [call] = calls_in([made("t1", "Bash", tool_input)]).values()
    assert fits(preview(tool_input), call)
    unicode_input = {"command": "echo 你好 世界 这是一个很长的命令行"}
    [other] = calls_in([made("t2", "Bash", unicode_input)]).values()
    assert fits(preview(unicode_input), other)


def test_a_call_with_nothing_long_enough_to_compare_fits_nothing() -> None:
    short = {"command": "ls", "timeout": 5000, "run_in_background": False}
    [call] = calls_in([made("t1", "Bash", short)]).values()
    assert call.needles == ()
    assert not fits(preview(short), call)


def test_every_long_string_input_has_to_be_shown() -> None:
    [call] = calls_in([made("t1", "Bash", BUILD)]).values()
    assert not fits('{ "command": "rm -rf build && make all" }', call), "the description is gone"
    assert not fits(preview(BUILD).replace("everything", "[REDACTED]"), call), "or was redacted"


def test_a_long_value_is_compared_by_its_head_since_the_preview_cuts_it() -> None:
    body = {"file_path": "/repo/src/module/long_file_name.py", "content": "x = 1\n" * 2000}
    [call] = calls_in([made("t1", "Write", body)]).values()
    cut = {**body, "content": body["content"][:2000] + "\n⋯ 8000 code points elided ⋯\n" + "y"}
    assert fits(preview(cut), call)
    assert not fits(preview({**cut, "file_path": "/repo/src/other/long_file_name.py"}), call)


def test_two_edits_of_one_file_are_told_apart_by_what_they_change() -> None:
    first = {"file_path": "/repo/src/app/main.py", "old_string": "timeout = 5", "new_string": "x"}
    other = {"file_path": "/repo/src/app/main.py", "old_string": "retries = 1", "new_string": "y"}
    found = calls_in([made("a", "Edit", first), made("b", "Edit", other)])
    assert fits(preview(first), found["a"]) and not fits(preview(first), found["b"])
    assert fits(preview(other), found["b"]) and not fits(preview(other), found["a"])


def test_a_prefix_of_a_longer_command_is_not_that_command() -> None:
    short = {"command": "git commit -m 'fix the build'"}
    [call] = calls_in([made("t1", "Bash", short)]).values()
    assert fits(preview(short), call)
    assert not fits(preview({"command": "git commit -m 'fix the build' && git push"}), call)


# ----------------------------------------------------------- the calls a prompt may be about


def test_a_prompt_is_about_the_open_calls_of_its_tool_that_it_shows_and_that_are_recent() -> None:
    now = time.time()
    rows = [
        made("mine", "Bash", BUILD, at=now - 5),
        made("closed", "Bash", BUILD, at=now - 6),
        made("other-tool", "Write", BUILD, at=now - 5),
        made("other-text", "Bash", {"command": "make test && make lint all"}, at=now - 5),
        made("ancient", "Bash", BUILD, at=now - LOOKBACK - 60),
        made("future", "Bash", BUILD, at=now + LOOKAHEAD + 60),
        ended("closed"),
    ]
    found = calls_in(rows)
    assert [call.id for call in open_calls(found, "Bash", preview(BUILD), now)] == ["mine"]
    assert [call.id for call in open_calls(found, "Bash", preview(BUILD), now, {"mine"})] == []


def test_a_call_made_well_after_the_prompt_is_not_its_call() -> None:
    """The row can lag the prompt by a moment, not by the time another call takes to be made."""
    now = time.time()
    assert LOOKAHEAD <= 10.0
    found = calls_in(
        [made("soon", "Bash", BUILD, at=now + 1), made("later", "Bash", BUILD, at=now + 15)]
    )
    assert [call.id for call in open_calls(found, "Bash", preview(BUILD), now)] == ["soon"]


def test_the_oldest_comes_first_and_a_call_with_no_time_is_always_recent() -> None:
    now = time.time()
    found = calls_in(
        [
            made("b", "Bash", BUILD, at=now - 1),
            made("a", "Bash", BUILD, at=now - 9),
            made("n", "Bash", BUILD),
        ]
    )
    assert [call.id for call in open_calls(found, "Bash", preview(BUILD), now)] == ["n", "a", "b"]


def test_a_plan_or_a_question_is_matched_by_its_tool_alone() -> None:
    now = time.time()
    found = calls_in([made("p", "ExitPlanMode", {"plan": "Step one of the plan"}, at=now)])
    assert [call.id for call in open_calls(found, "ExitPlanMode", None, now)] == ["p"]


# --------------------------------------------------------------- when to pair them


@dataclass(slots=True)
class Prompt:
    """Anything that waits on the person: the same fields an approval has."""

    tool: str
    preview: str
    raised: float
    plan: dict[str, Any] | None = None
    call: str | None = None
    input: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.input = {"input_preview": self.preview}


def open_ones(now: float, *rows: dict[str, Any]) -> dict[str, Call]:
    return calls_in([made(f"t{n}", "Bash", BUILD, at=now - 2 + n * 0.1) for n in range(len(rows))])


def test_a_prompt_that_fits_one_call_is_its_prompt() -> None:
    now = time.time()
    found = calls_in([made("t1", "Bash", BUILD, at=now - 2)])
    prompt = Prompt("Bash", preview(BUILD), now)
    bind_approvals([prompt], found, {}, set())
    assert prompt.call == "t1"


def test_a_call_that_is_only_running_is_not_known_to_be_the_one_asked_about() -> None:
    now = time.time()
    found = open_ones(now, {}, {})
    prompt = Prompt("Bash", preview(BUILD), now)
    bind_approvals([prompt], found, {}, set())
    assert prompt.call is None, "two calls fit and one prompt: either may be only running"


def test_prompts_that_look_alike_are_paired_with_nothing() -> None:
    """Which card is whose cannot be told, and a wrong pairing closes the card still waiting."""
    now = time.time()
    found = open_ones(now, {}, {})
    first, second = Prompt("Bash", preview(BUILD), now), Prompt("Bash", preview(BUILD), now + 1)
    bind_approvals([second, first], found, {}, set())
    assert (first.call, second.call) == (None, None)


def test_more_prompts_than_calls_means_a_call_is_not_seen_and_none_is_paired() -> None:
    now = time.time()
    found = open_ones(now, {})
    first, second = Prompt("Bash", preview(BUILD), now), Prompt("Bash", preview(BUILD), now + 1)
    bind_approvals([first, second], found, {}, set())
    assert (first.call, second.call) == (None, None)


def test_prompts_that_differ_are_each_paired_with_their_own_call() -> None:
    now = time.time()
    other = {"command": "npm test", "description": "Run the unit tests"}
    found = calls_in([made("t1", "Bash", BUILD, at=now - 2), made("t2", "Bash", other, at=now - 1)])
    build, test = Prompt("Bash", preview(BUILD), now), Prompt("Bash", preview(other), now)
    bind_approvals([build, test], found, {}, set())
    assert (build.call, test.call) == ("t1", "t2")


def test_a_call_already_paired_is_not_paired_again() -> None:
    now = time.time()
    found = open_ones(now, {}, {})
    prompt = Prompt("Bash", preview(BUILD), now)
    bind_approvals([prompt], found, {}, {"t0"})
    assert prompt.call == "t1"


def test_a_prompt_the_main_thread_may_have_raised_is_paired_with_nothing() -> None:
    """The main thread's prompts come the same way, and its call is open in its transcript."""
    now = time.time()
    found = calls_in([made("t1", "Bash", BUILD, at=now - 20)])
    rivals = calls_in([made("m1", "Bash", BUILD, at=now - 1)])
    prompt = Prompt("Bash", preview(BUILD), now)
    bind_approvals([prompt], found, rivals, set())
    assert prompt.call is None

    # A main-thread call that ended, or is about something else, is no rival.
    done = calls_in([made("m1", "Bash", BUILD, at=now - 1), ended("m1")])
    other = calls_in([made("m2", "Bash", {"command": "git status --short"}, at=now - 1)])
    for harmless in (done, other):
        prompt = Prompt("Bash", preview(BUILD), now)
        bind_approvals([prompt], found, harmless, set())
        assert prompt.call == "t1"


def test_a_prompt_with_no_preview_is_paired_with_nothing() -> None:
    now = time.time()
    found = calls_in([made("t1", "Bash", BUILD, at=now - 2)])
    prompt = Prompt("Bash", "", now)
    bind_approvals([prompt], found, {}, set())
    assert prompt.call is None


def test_a_plan_is_paired_by_its_tool_and_a_question_by_the_one_open_call() -> None:
    now = time.time()
    found = calls_in(
        [
            made("p", "ExitPlanMode", {"plan": "Step one of the plan"}, at=now - 2),
            made("q", "AskUserQuestion", {"questions": []}, at=now - 2),
        ]
    )
    plan = Prompt("ExitPlanMode", "", now, plan={"title": "Plan"})
    bind_approvals([plan], found, {}, set())
    assert plan.call == "p"
    question = Prompt("", "", now)
    bind_question(question, found, {}, set(), "AskUserQuestion")
    assert question.call == "q"
    two = calls_in(
        [made("a", "AskUserQuestion", {}, at=now - 2), made("b", "AskUserQuestion", {}, at=now - 1)]
    )
    another = Prompt("", "", now)
    bind_question(another, two, {}, set(), "AskUserQuestion")
    assert another.call is None


def test_a_question_the_main_thread_may_have_asked_is_paired_with_nothing() -> None:
    now = time.time()
    found = calls_in([made("q", "AskUserQuestion", {"questions": []}, at=now - 2)])
    rivals = calls_in([made("m", "AskUserQuestion", {"questions": []}, at=now - 1)])
    question = Prompt("", "", now)
    bind_question(question, found, rivals, set(), "AskUserQuestion")
    assert question.call is None
    bind_question(question, found, {}, set(), "AskUserQuestion")
    assert question.call == "q"


def test_the_end_of_a_paired_call_answers_its_prompt_only_while_no_other_call_could_be_it() -> None:
    now = time.time()
    prompt = Prompt("Bash", preview(BUILD), now, call="t1")
    only = calls_in([made("t1", "Bash", BUILD, at=now - 2), ended("t1")])
    assert not contested_approval(prompt, only, {})

    another = calls_in(
        [made("t1", "Bash", BUILD, at=now - 2), ended("t1"), made("t2", "Bash", BUILD, at=now - 1)]
    )
    assert contested_approval(prompt, another, {}), "a later identical call is still open"
    rival = calls_in([made("m1", "Bash", BUILD, at=now - 1)])
    assert contested_approval(prompt, only, rival), "and so is the main thread's"
    question = Prompt("", "", now, call="q1")
    asked = calls_in([made("q1", "AskUserQuestion", {}, at=now - 2), ended("q1")])
    assert not contested_question(question, asked, {}, "AskUserQuestion")
    asked.update(calls_in([made("q2", "AskUserQuestion", {}, at=now - 1)]))
    assert contested_question(question, asked, {}, "AskUserQuestion")


OTHER = {"command": "git status --short", "description": "Show what changed"}


def test_with_no_subagent_a_result_answers_the_only_prompt_of_its_tool() -> None:
    now = time.time()
    build = Prompt("Bash", preview(BUILD), now)
    write = Prompt("Write", preview({"file_path": "/repo/x.md", "content": "hello there"}), now)
    assert answered_by_result([build, write], "Bash", OTHER, False) is build
    assert answered_by_result([build, write], "Bash", BUILD, False) is build
    assert answered_by_result([build, write], "Edit", OTHER, False) is None


def test_with_no_subagent_the_prompt_that_shows_the_input_is_the_one_answered() -> None:
    now = time.time()
    build, status = Prompt("Bash", preview(BUILD), now), Prompt("Bash", preview(OTHER), now)
    assert answered_by_result([build, status], "Bash", OTHER, False) is status
    assert answered_by_result([build, status], "Bash", {"command": "pwd"}, False) is None


def test_with_subagents_about_a_result_answers_only_a_prompt_that_shows_its_input() -> None:
    now = time.time()
    build, status = Prompt("Bash", preview(BUILD), now), Prompt("Bash", preview(OTHER), now)
    assert answered_by_result([build, status], "Bash", OTHER, True) is status
    assert answered_by_result([build], "Bash", OTHER, True) is None, "it is some subagent's"


def test_an_input_with_nothing_to_compare_answers_the_one_prompt_there_is() -> None:
    now = time.time()
    short = {"command": "pwd"}
    one, two = Prompt("Bash", preview(short), now), Prompt("Bash", preview({"command": "ls"}), now)
    assert answered_by_result([one], "Bash", short, True) is one
    assert answered_by_result([one, two], "Bash", short, True) is None


@pytest.mark.parametrize("subagents", [False, True])
def test_prompts_that_look_alike_are_answered_by_no_result(subagents: bool) -> None:
    now = time.time()
    first, second = Prompt("Bash", preview(BUILD), now), Prompt("Bash", preview(BUILD), now + 1)
    assert answered_by_result([first, second], "Bash", BUILD, subagents) is None


def test_a_prompt_paired_with_a_subagents_call_is_never_answered_by_the_main_threads_result() -> (
    None
):
    paired = Prompt("Bash", preview(BUILD), time.time(), call="t1")
    assert answered_by_result([paired], "Bash", BUILD, False) is None
    assert answered_by_result([paired], "Bash", BUILD, True) is None


@pytest.mark.parametrize("subagents", [False, True])
def test_a_plan_is_answered_by_its_tool_alone(subagents: bool) -> None:
    plan = Prompt("ExitPlanMode", "", time.time(), plan={"title": "Plan"})
    assert answered_by_result([plan], "ExitPlanMode", {"plan": "Step one"}, subagents) is plan


def test_the_end_of_a_call_does_not_count_the_call_the_prompt_is_paired_with() -> None:
    """The open call it is paired with fits it, and is no reason to let the pairing go."""
    now = time.time()
    prompt = Prompt("Bash", preview(BUILD), now, call="t1")
    open_one = calls_in([made("t1", "Bash", BUILD, at=now - 2)])
    assert not contested_approval(prompt, open_one, {})
    both = calls_in([made("t1", "Bash", BUILD, at=now - 2), made("t2", "Bash", BUILD, at=now - 1)])
    assert contested_approval(prompt, both, {})
    asked = Prompt("", "", now, call="q1")
    questions = calls_in([made("q1", "AskUserQuestion", {}, at=now - 2)])
    assert not contested_question(asked, questions, {}, "AskUserQuestion")


def test_a_main_thread_call_is_the_one_asked_about_when_its_input_is_in_the_preview() -> None:
    now = time.time()
    prompt = Prompt("Bash", preview(BUILD), now)
    assert about_input(prompt, BUILD)
    assert not about_input(prompt, {"command": "ls -la /tmp", "description": "List the files"})
    assert not about_input(prompt, {"command": "ls"}), "nothing long enough to tell it by"
    assert not about_input(prompt, None)
    plan = Prompt("ExitPlanMode", "", now, plan={"title": "Plan"})
    assert about_input(plan, {"plan": "whatever"}), "a plan is the main thread's, by its tool"
