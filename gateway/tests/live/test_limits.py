"""The command rate and the daily call time."""

from __future__ import annotations

import pytest

from rc_gateway.live.budget import DailyBudget
from rc_gateway.live.errors import LiveError
from rc_gateway.live.limits import COMMAND_BURST, COMMANDS_PER_SECOND, CommandGate

from .fakes import FakeClock


def test_a_burst_is_allowed_and_then_the_rate() -> None:
    clock = FakeClock()
    gate = CommandGate(clock=clock)
    for _ in range(COMMAND_BURST):
        gate.check()
    with pytest.raises(LiveError) as refused:
        gate.check()
    assert refused.value.code == "protocol_error"
    # A tenth of a second later two more are allowed, no more.
    clock.advance(2 / COMMANDS_PER_SECOND)
    gate.check()
    gate.check()
    with pytest.raises(LiveError):
        gate.check()


def test_a_person_pressing_buttons_never_meets_it() -> None:
    clock = FakeClock()
    gate = CommandGate(clock=clock)
    for _ in range(500):
        gate.check()
        clock.advance(0.1)


def test_the_bucket_never_holds_more_than_its_burst() -> None:
    clock = FakeClock()
    gate = CommandGate(clock=clock)
    clock.advance(10_000)
    for _ in range(COMMAND_BURST):
        gate.check()
    with pytest.raises(LiveError):
        gate.check()


DAY = 86_400.0
NOON = 1_800_000_000.0 - (1_800_000_000.0 % DAY) + DAY / 2


def test_an_account_has_the_days_time_less_what_it_used() -> None:
    wall = FakeClock(NOON)
    budget = DailyBudget(7200, wall_clock=wall)
    assert budget.remaining("admin") == 7200
    budget.charge("admin", 1200)
    budget.charge("admin", 600)
    assert budget.remaining("admin") == 5400
    assert budget.remaining("dana") == 7200


def test_the_time_used_never_goes_below_nothing_left() -> None:
    budget = DailyBudget(100, wall_clock=FakeClock(NOON))
    budget.charge("admin", 5_000)
    assert budget.remaining("admin") == 0
    budget.charge("admin", -50)
    assert budget.remaining("admin") == 0


def test_a_new_utc_day_starts_afresh() -> None:
    wall = FakeClock(NOON)
    budget = DailyBudget(100, wall_clock=wall)
    budget.charge("admin", 100)
    assert budget.remaining("admin") == 0
    wall.advance(DAY / 2 + 1)
    assert budget.remaining("admin") == 100


def test_the_same_day_keeps_the_count_through_the_hours() -> None:
    wall = FakeClock(NOON)
    budget = DailyBudget(100, wall_clock=wall)
    budget.charge("admin", 60)
    wall.advance(3 * 3600)
    assert budget.remaining("admin") == 40


def test_the_accounts_it_remembers_are_bounded() -> None:
    budget = DailyBudget(100, wall_clock=FakeClock(NOON))
    for index in range(2000):
        budget.charge(f"user{index}", 10)
    assert len(budget._used) <= 1024
