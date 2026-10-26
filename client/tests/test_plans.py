"""`ExitPlanMode` as a plan card (A54): the block, the verdicts and the SDK path."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from rc_client import events
from rc_client.agents.claude import plans
from rc_client.agents.claude.adapter import ClaudeRunner
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel

PLAN = "# Add retries\n\n- wrap the client\n- cap the delay\n"


def test_a_plan_block_carries_the_plan_once_and_three_options() -> None:
    fields = plans.plan_fields(PLAN, False)
    assert fields["approval_kind"] == "plan"
    assert fields["input"] == {"plan": PLAN}
    assert fields["title"] == "Plan ready: Add retries"
    assert [option["id"] for option in fields["options"]] == [
        "plan_accept_edits",
        "plan_manual",
        "plan_keep",
    ]
    assert [option["style"] for option in fields["options"]] == ["primary", "secondary", "danger"]
    assert "plan_truncated" not in fields


def test_a_plan_too_big_for_the_frame_is_cut_and_offers_no_approval() -> None:
    big = 'line of a very long plan, with "quotes"\n' * 5000
    fitted, truncated = plans.fit_plan(big)
    assert truncated is True
    assert len(json.dumps(fitted, ensure_ascii=False).encode("utf-8")) <= plans.MAX_PLAN_JSON_BYTES
    fields = plans.plan_fields(fitted, truncated)
    assert fields["plan_truncated"] is True
    assert [option["id"] for option in fields["options"]] == ["plan_shorten", "plan_keep"]
    event = events.bound_event({"kind": "approval", "seq": 1, "status": "pending", **fields})
    assert event["input"]["plan"] == fitted
    assert len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= events.MAX_EVENT_BYTES


def test_a_wide_character_plan_is_fitted_by_its_encoded_size() -> None:
    fitted, truncated = plans.fit_plan("计划" * 20000)
    assert truncated is True
    assert len(json.dumps(fitted, ensure_ascii=False).encode("utf-8")) <= plans.MAX_PLAN_JSON_BYTES


def test_a_small_plan_passes_through_whole() -> None:
    assert plans.fit_plan(PLAN) == (PLAN, False)


@pytest.mark.parametrize(
    ("option", "message", "expected"),
    [
        ("plan_accept_edits", None, {"behavior": "allow", "mode": "acceptEdits"}),
        ("plan_manual", "ignored", {"behavior": "allow", "mode": "default"}),
    ],
)
def test_approving_names_the_mode_the_session_goes_on_in(
    option: str, message: str | None, expected: dict[str, Any]
) -> None:
    assert plans.verdict(option, message, False) == expected


def test_keeping_on_planning_tells_claude_what_to_change() -> None:
    outcome = plans.verdict("plan_keep", "  split step two  ", False)
    assert outcome["behavior"] == "deny"
    assert outcome["interrupt"] is False
    assert "split step two" in outcome["message"]


def test_keeping_on_planning_with_nothing_to_say_interrupts_the_turn() -> None:
    for message in (None, "", "   "):
        outcome = plans.verdict("plan_keep", message, False)
        assert outcome["behavior"] == "deny"
        assert outcome["interrupt"] is True


def test_an_option_the_block_never_offered_is_never_consent() -> None:
    assert plans.verdict("allow", None, False)["behavior"] == "deny"
    assert plans.verdict("plan_accept_edits", None, True)["behavior"] == "deny"
    assert plans.verdict("plan_shorten", None, True)["interrupt"] is False


# ------------------------------------------------------------- the plan file


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "claude-config"
    (root / "plans").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


def test_a_plan_file_under_the_plans_directory_is_read(config_dir: Path) -> None:
    path = config_dir / "plans" / "bold-fox.md"
    path.write_text(PLAN, encoding="utf-8")
    assert plans.plan_from_file({"planFilePath": str(path)}) == PLAN


def test_a_plan_file_elsewhere_is_never_read(config_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secrets.md"
    outside.write_text("hunter2", encoding="utf-8")
    assert plans.plan_from_file({"planFilePath": str(outside)}) is None
    link = config_dir / "plans" / "sneaky.md"
    link.symlink_to(outside)
    assert plans.plan_from_file({"planFilePath": str(link)}) is None
    escaped = config_dir / "plans" / ".." / ".." / "secrets.md"
    assert plans.plan_from_file({"planFilePath": str(escaped)}) is None


def test_only_a_regular_markdown_file_counts(config_dir: Path) -> None:
    text = config_dir / "plans" / "plan.txt"
    text.write_text(PLAN, encoding="utf-8")
    folder = config_dir / "plans" / "folder.md"
    folder.mkdir()
    for named in (str(text), str(folder), str(config_dir / "plans" / "missing.md"), "", None, 3):
        assert plans.plan_from_file({"planFilePath": named}) is None


def test_the_plan_in_the_input_wins() -> None:
    assert plans.plan_text({"plan": PLAN}) == PLAN
    assert plans.plan_text({"plan": "   "}) is None
    assert plans.plan_text({}) is None


# ------------------------------------------------------------- the SDK path


def runner_for(tmp_path: Path, frames: list[dict[str, Any]]) -> ClaudeRunner:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="claude", cwd=str(tmp_path))
    registry.upsert_session(session)

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    channel = SessionChannel(registry, session, publish)
    return ClaudeRunner(channel, binary=None, cwd=str(tmp_path))


async def answered(
    runner: ClaudeRunner, tool_input: dict[str, Any], option_id: str, message: str | None = None
) -> PermissionResultAllow | PermissionResultDeny:
    async def reply() -> None:
        while not runner._pending:
            await asyncio.sleep(0.01)
        await runner.approve(next(iter(runner._pending)), option_id, message)

    task = asyncio.create_task(reply())
    result = await runner._can_use_tool(plans.PLAN_TOOL, tool_input, ToolPermissionContext())
    await task
    return result


def approvals(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"]["kind"] == "approval"
    ]


async def test_a_plan_is_raised_as_a_plan_card_and_approved_into_accept_edits(
    tmp_path: Path,
) -> None:
    frames: list[dict[str, Any]] = []
    runner = runner_for(tmp_path, frames)
    result = await answered(runner, {"plan": PLAN}, "plan_accept_edits")

    assert isinstance(result, PermissionResultAllow)
    [update] = result.updated_permissions or []
    assert (update.type, update.mode, update.destination) == ("setMode", "acceptEdits", "session")
    pending, resolved = approvals(frames)
    assert pending["approval_kind"] == "plan"
    assert pending["tool"] == "ExitPlanMode"
    assert pending["input"] == {"plan": PLAN}
    assert resolved["decision"] == {"option_id": "plan_accept_edits", "by": "remote"}
    assert runner.channel.session.permission_mode == "acceptEdits"
    runner.channel.registry.close()


async def test_a_plan_approved_for_manual_edits_sets_the_default_mode(tmp_path: Path) -> None:
    runner = runner_for(tmp_path, [])
    result = await answered(runner, {"plan": PLAN}, "plan_manual")
    assert isinstance(result, PermissionResultAllow)
    [update] = result.updated_permissions or []
    assert update.mode == "default"
    runner.channel.registry.close()


async def test_keeping_on_planning_denies_with_the_feedback(tmp_path: Path) -> None:
    runner = runner_for(tmp_path, [])
    result = await answered(runner, {"plan": PLAN}, "plan_keep", "use a queue instead")
    assert isinstance(result, PermissionResultDeny)
    assert "use a queue instead" in result.message
    assert result.interrupt is False
    runner.channel.registry.close()


async def test_keeping_on_planning_without_feedback_interrupts(tmp_path: Path) -> None:
    runner = runner_for(tmp_path, [])
    result = await answered(runner, {"plan": PLAN}, "plan_keep")
    assert isinstance(result, PermissionResultDeny)
    assert result.interrupt is True
    runner.channel.registry.close()


async def test_an_unoffered_option_on_a_plan_is_a_refusal(tmp_path: Path) -> None:
    runner = runner_for(tmp_path, [])
    result = await answered(runner, {"plan": PLAN}, "allow")
    assert isinstance(result, PermissionResultDeny)
    runner.channel.registry.close()


async def test_a_plan_the_input_lacks_is_read_from_its_file(
    tmp_path: Path, config_dir: Path
) -> None:
    path = config_dir / "plans" / "bold-fox.md"
    path.write_text(PLAN, encoding="utf-8")
    frames: list[dict[str, Any]] = []
    runner = runner_for(tmp_path, frames)
    await answered(runner, {"planFilePath": str(path)}, "plan_manual")
    assert approvals(frames)[0]["input"] == {"plan": PLAN}
    runner.channel.registry.close()


async def test_a_plan_found_nowhere_is_the_ordinary_approval(tmp_path: Path) -> None:
    frames: list[dict[str, Any]] = []
    runner = runner_for(tmp_path, frames)
    result = await answered(runner, {}, "allow")
    assert isinstance(result, PermissionResultAllow)
    assert "approval_kind" not in approvals(frames)[0]
    runner.channel.registry.close()


def test_a_plan_file_past_the_size_cap_is_not_a_plan(config_dir: Path) -> None:
    path = config_dir / "plans" / "huge.md"
    path.write_text("x" * (plans.MAX_PLAN_FILE_BYTES + 1), encoding="utf-8")
    assert plans.plan_from_file({"planFilePath": str(path)}) is None
    path.write_text("x" * plans.MAX_PLAN_FILE_BYTES, encoding="utf-8")
    assert plans.plan_from_file({"planFilePath": str(path)}) is not None


def test_a_pipe_named_as_the_plan_file_is_never_waited_on(config_dir: Path) -> None:
    import os

    path = config_dir / "plans" / "pipe.md"
    os.mkfifo(path)
    assert plans.plan_from_file({"planFilePath": str(path)}) is None


def test_a_platform_without_nofollow_or_nonblock_still_reads_the_plan(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    path = config_dir / "plans" / "bold-fox.md"
    path.write_text(PLAN, encoding="utf-8")
    assert plans.plan_from_file({"planFilePath": str(path)}) == PLAN
