"""A35 for Antigravity: words only ask `agy`'s usage query; a window at zero is the limit (A64)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.antigravity import limit
from rc_client.agents.antigravity import runner as runner_module
from rc_client.agents.antigravity.usage import Bucket, Group, Reading
from tests.agy_helpers import Recorder, entry_for, fake_agy, runner_for, wait_for

GEMINI = ("Gemini 4 Neon", "Gemini 3.8 Flash")
OTHERS = ("Claude Opus 5.5", "GPT-OSS 120B")


def group(name: str, models: tuple[str, ...], *buckets: Bucket) -> Group:
    return Group(name, models, tuple(buckets))


def answer(five: float, weekly: float) -> dict[str, Any]:
    return {
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "description": "Models within this group: Gemini 4 Neon, Gemini 3.8 Flash",
                        "buckets": [
                            {
                                "window": "5h",
                                "remaining_fraction": five,
                                "reset_time": "2026-11-22T16:12:59Z",
                            },
                            {
                                "window": "weekly",
                                "remaining_fraction": weekly,
                                "reset_time": "2026-11-29T11:12:59Z",
                            },
                        ],
                    }
                ]
            },
        }
    }


@pytest.mark.parametrize(
    "words",
    [
        "You have exhausted your quota for this model",
        "RESOURCE_EXHAUSTED: try later",
        "reason: QUOTA_EXHAUSTED",
        "Your individual quota reached its limit",
        "Usage Limit Reached",
    ],
)
def test_words_that_read_like_agy_s_quota_message(words: str) -> None:
    assert limit.reads_like_quota(words)


def test_ordinary_words_ask_nothing() -> None:
    assert not limit.reads_like_quota("Model overloaded", "", "quota is a word")


def test_the_group_that_names_the_model_decides() -> None:
    reading = Reading(
        (
            group("Gemini Models", GEMINI, Bucket(300, 0.4, 1000), Bucket(10080, 0.9, 5000)),
            group("Claude and GPT models", OTHERS, Bucket(300, 0.0, 2000)),
        ),
        0,
    )
    assert limit.used_up(reading, "Gemini 4 Neon") is None, "the other group's is not this one's"
    found = limit.used_up(reading, "GPT-OSS 120B (Medium)")
    assert found is not None and (found.window_minutes, found.resets_at_ms) == (300, 2000)


def test_any_group_when_none_names_the_model_and_the_later_reset_of_two() -> None:
    reading = Reading(
        (group("Gemini Models", GEMINI, Bucket(300, 0.0, 1000), Bucket(10080, 0.0, 5000)),),
        0,
    )
    found = limit.used_up(reading, "A model nobody lists")
    assert found is not None and (found.window_minutes, found.resets_at_ms) == (10080, 5000)
    assert limit.used_up(reading, None) is not None


def test_nothing_used_up_is_no_limit() -> None:
    reading = Reading((group("Gemini Models", GEMINI, Bucket(300, 0.01, 1000)),), 0)
    assert limit.used_up(reading, "Gemini 4 Neon") is None


async def run_turn(tmp_path: Path, turn: str, **config: Any) -> tuple[Recorder, Any]:
    binary, peer = fake_agy(tmp_path, turns=[turn], **config)
    recorder = Recorder()
    runner = runner_for(entry_for(tmp_path, recorder), binary)
    await runner.start()
    await runner.send("Go.")
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()
    return recorder, peer


def queries(peer: Any) -> int:
    return sum(1 for start in peer.starts() if start["argv"][:2] == ["-p", "/usage"])


async def test_a_quota_error_with_a_window_used_up_ends_the_turn_at_the_limit(
    tmp_path: Path,
) -> None:
    recorder, peer = await run_turn(tmp_path, "quota", usage=answer(0.0, 0.5))
    [completed] = recorder.events("turn_completed")
    assert completed["stop_reason"] == "error"
    assert completed["limit"] == {"window_minutes": 300, "resets_at": 1795363979000}
    assert [e["message"] for e in recorder.events("error")] == [
        "You have exhausted your quota for this model"
    ], "agy's words stay where agy put them"
    assert queries(peer) == 1


async def test_quota_words_in_agy_s_text_are_checked_the_same_way(tmp_path: Path) -> None:
    recorder, _ = await run_turn(tmp_path, "quota-text", usage=answer(0.3, 0.0))
    [completed] = recorder.events("turn_completed")
    assert (completed["stop_reason"], completed["limit"]["window_minutes"]) == ("error", 10080)
    assert recorder.events("assistant_text")[-1]["text"] == "Usage Limit Reached for Gemini Models."
    assert not recorder.events("error")


async def test_quota_words_with_every_window_left_end_the_turn_as_they_would(
    tmp_path: Path,
) -> None:
    recorder, peer = await run_turn(tmp_path, "quota-text", usage=answer(0.3, 0.6))
    [completed] = recorder.events("turn_completed")
    assert completed["stop_reason"] == "completed" and "limit" not in completed
    assert queries(peer) == 1


async def test_a_query_that_fails_confirms_nothing(tmp_path: Path) -> None:
    recorder, _ = await run_turn(tmp_path, "quota", usage_fails=True)
    [completed] = recorder.events("turn_completed")
    assert completed["stop_reason"] == "error" and "limit" not in completed


async def test_other_failures_ask_no_query(tmp_path: Path) -> None:
    _, peer = await run_turn(tmp_path, "fail")
    assert queries(peer) == 0


async def test_the_limit_reaches_the_resume_scheduler(tmp_path: Path) -> None:
    binary, _ = fake_agy(tmp_path, turns=["quota"], usage=answer(0.0, 0.5))
    recorder = Recorder()
    entry = entry_for(tmp_path, recorder)
    seen: list[Any] = []

    async def on_turn_end(session: Any, stop: Any) -> None:
        seen.append(stop)

    entry.channel.on_turn_end = on_turn_end
    runner = runner_for(entry, binary)
    await runner.start()
    await runner.send("Go.")
    await wait_for(lambda: seen)
    await runner.close()
    assert seen[0] is not None and seen[0].window_minutes == 300


def test_the_quiet_after_a_wake_is_three_seconds() -> None:
    assert runner_module.WAKE_QUIET == 3.0
