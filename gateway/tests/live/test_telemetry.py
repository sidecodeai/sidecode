"""The browser player's own report: kept as reported, bounded, and never an error."""

from __future__ import annotations

from typing import Any

import pytest

from rc_gateway.live.telemetry import MAX_DELAY_MS, MAX_REPORTED, PlaybackReport

NEVER = {
    "browser_underruns": None,
    "browser_gap_ms": None,
    "browser_restarts": None,
    "browser_delay_ms": None,
}


def report(**fields: Any) -> dict[str, Any]:
    return {"type": "playback", **fields}


def full(
    underruns: int = 0, gap_ms: int = 0, restarts: int = 0, delay_ms: int = 400
) -> dict[str, Any]:
    return report(underruns=underruns, gap_ms=gap_ms, restarts=restarts, delay_ms=delay_ms)


def test_a_browser_that_never_reported_is_null_in_every_field() -> None:
    telemetry = PlaybackReport()
    assert telemetry.record() == NEVER
    assert telemetry.delay_seconds is None


def test_the_latest_report_is_the_one_kept() -> None:
    telemetry = PlaybackReport()
    telemetry.note(full(1, 120, 0, 400))
    telemetry.note(full(3, 900, 2, 600))
    assert telemetry.record() == {
        "browser_underruns": 3,
        "browser_gap_ms": 900,
        "browser_restarts": 2,
        "browser_delay_ms": 600,
    }
    assert telemetry.delay_seconds == pytest.approx(0.6)


def test_zero_is_a_report_not_nothing() -> None:
    telemetry = PlaybackReport()
    telemetry.note(full(0, 0, 0, 0))
    assert telemetry.record() == {
        "browser_underruns": 0,
        "browser_gap_ms": 0,
        "browser_restarts": 0,
        "browser_delay_ms": 0,
    }
    assert telemetry.delay_seconds == 0.0


BAD_COUNTERS = [
    {"underruns": -1, "gap_ms": 0, "restarts": 0},
    {"underruns": 1.5, "gap_ms": 0, "restarts": 0},
    {"underruns": 1.0, "gap_ms": 0, "restarts": 0},
    {"underruns": True, "gap_ms": 0, "restarts": 0},
    {"underruns": "3", "gap_ms": 0, "restarts": 0},
    {"underruns": None, "gap_ms": 0, "restarts": 0},
    {"underruns": [1], "gap_ms": 0, "restarts": 0},
    {"underruns": 1, "gap_ms": 0},
    {},
]


@pytest.mark.parametrize("bad", BAD_COUNTERS)
def test_counters_that_are_not_three_non_negative_integers_are_ignored_together(
    bad: dict[str, Any],
) -> None:
    telemetry = PlaybackReport()
    telemetry.note(report(**bad))
    assert telemetry.record() == NEVER
    telemetry.note(full(4, 5, 6, 700))
    telemetry.note(report(**bad))
    assert telemetry.record() == {
        "browser_underruns": 4,
        "browser_gap_ms": 5,
        "browser_restarts": 6,
        "browser_delay_ms": 700,
    }


def test_a_count_beyond_anything_real_is_clamped() -> None:
    telemetry = PlaybackReport()
    telemetry.note(report(underruns=10**30, gap_ms=MAX_REPORTED + 1, restarts=MAX_REPORTED))
    record = telemetry.record()
    assert [record[f"browser_{name}"] for name in ("underruns", "gap_ms", "restarts")] == [
        MAX_REPORTED
    ] * 3


# ---- the playout delay ----


@pytest.mark.parametrize(
    ("sent", "kept"), [(0, 0), (400, 400), (2000, 2000), (2001, 2000), (10**9, 2000)]
)
def test_a_delay_from_nothing_to_two_seconds_is_kept_and_a_longer_one_is_clamped(
    sent: int, kept: int
) -> None:
    telemetry = PlaybackReport()
    telemetry.note(full(delay_ms=sent))
    assert telemetry.record()["browser_delay_ms"] == kept
    assert telemetry.delay_seconds == kept / 1000
    assert MAX_DELAY_MS == 2000


@pytest.mark.parametrize("junk", [-1, 0.4, 400.0, "400", True, False, None, [400], {"ms": 400}])
def test_a_delay_that_is_not_a_non_negative_integer_is_ignored(junk: Any) -> None:
    telemetry = PlaybackReport()
    telemetry.note(full(delay_ms=600))
    telemetry.note(full(delay_ms=junk))
    assert telemetry.record()["browser_delay_ms"] == 600
    fresh = PlaybackReport()
    fresh.note(full(delay_ms=junk))
    assert fresh.record()["browser_delay_ms"] is None
    assert fresh.delay_seconds is None


def test_a_report_without_a_delay_leaves_the_delay_as_it_was() -> None:
    telemetry = PlaybackReport()
    telemetry.note(full(delay_ms=800))
    telemetry.note(report(underruns=1, gap_ms=2, restarts=3))
    assert telemetry.record()["browser_delay_ms"] == 800
    assert telemetry.record()["browser_underruns"] == 1


def test_the_delay_stands_on_its_own_and_the_counters_stand_together() -> None:
    telemetry = PlaybackReport()
    # Bad counters do not take a good delay down with them...
    telemetry.note(report(underruns=-1, gap_ms=5, restarts=1, delay_ms=900))
    assert telemetry.record() == {**NEVER, "browser_delay_ms": 900}
    # ...and a bad delay does not take good counters down.
    telemetry.note(report(underruns=2, gap_ms=5, restarts=1, delay_ms="soon"))
    assert telemetry.record() == {
        "browser_underruns": 2,
        "browser_gap_ms": 5,
        "browser_restarts": 1,
        "browser_delay_ms": 900,
    }
