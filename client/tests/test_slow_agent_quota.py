"""A request for quota an agent is too slow to answer follows the rule of every earlier reading.

The windows the agent last read are shown with no line over them while they may stand in
(`earlier_reading`: under thirty minutes old, none that has reset since, and an account that has
an email to tell it from another). An account that normally carries quota and has nothing recent
enough to show says that the device was too slow; any other stays as it was (A33).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest

from rc_client.agents import detection, registry
from rc_client.agents.registry import DetectContext, detect_all
from rc_client.agents.slow_agent import SLOW_AGENT, remember
from rc_client.models import AgentAccount, AgentInfo, AgentLimit
from tests.detect_helpers import after, everyone, install, reporting, settle
from tests.usage_helpers import READ_AT, Clock

HALF_HOUR = 30 * 60
WITH_QUOTA = DetectContext(limits=True)
EMAIL = "me@example.com"


def subscription(
    *windows: AgentLimit,
    read_at: int | None = READ_AT,
    email: str | None = EMAIL,
    provider: str = "anthropic",
) -> AgentAccount:
    """An account as a request for quota answers for it."""
    return AgentAccount(
        provider=provider,
        method="account",
        plan="max",
        email=email,
        limits=list(windows),
        limits_checked_at=read_at,
    )


def window(percent: int, minutes_to_reset: int | None = 120) -> AgentLimit:
    resets_at = None if minutes_to_reset is None else READ_AT + minutes_to_reset * 60_000
    return AgentLimit(300, percent, resets_at=resets_at)


def accounts_of(info: AgentInfo) -> list[AgentAccount]:
    assert info.accounts is not None
    return info.accounts


def slow(monkeypatch: pytest.MonkeyPatch, agent: str = "pi") -> asyncio.Event:
    """`agent` is too slow from now on, and every other agent answers at once."""
    gate = asyncio.Event()
    install(monkeypatch, {**everyone("2.0"), agent: after(gate, reporting(agent, "2.0"))})
    return gate


async def ask_slow(
    monkeypatch: pytest.MonkeyPatch,
    earlier: list[AgentAccount],
    later: float = 0,
    agent: str = "pi",
) -> AgentInfo:
    """`agent` answers a request for quota with `earlier`, and `later` seconds on is too slow to."""
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    clock = Clock()
    monkeypatch.setattr(detection, "now_ms", clock.now_ms)
    install(monkeypatch, everyone("1.0", **{agent: earlier}))
    await detect_all(WITH_QUOTA)
    clock.advance(later)
    gate = slow(monkeypatch, agent)

    agents = await detect_all(WITH_QUOTA)

    gate.set()
    await settle()
    return next(info for info in agents if info.agent == agent)


# ----------------------------------------------------------------- what is shown


async def test_a_slow_agent_shows_the_windows_it_last_read_with_no_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pi = await ask_slow(monkeypatch, [subscription(window(16), window(54, 3 * 24 * 60))], later=600)

    [account] = accounts_of(pi)

    assert account.limits == [window(16), window(54, 3 * 24 * 60)]
    assert account.limits_error is None
    assert account.limits_checked_at == READ_AT
    assert pi.version == "1.0", "the rest of what the agent last said stands too"


async def test_windows_thirty_minutes_old_are_not_shown_and_the_account_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pi = await ask_slow(monkeypatch, [subscription(window(16))], later=HALF_HOUR)

    [account] = accounts_of(pi)

    assert account.limits is None
    assert account.limits_error == SLOW_AGENT
    assert account.limits_checked_at == READ_AT + HALF_HOUR * 1000


async def test_windows_just_under_thirty_minutes_old_are_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pi = await ask_slow(monkeypatch, [subscription(window(16))], later=HALF_HOUR - 1)

    [account] = accounts_of(pi)

    assert account.limits == [window(16)]
    assert account.limits_error is None


async def test_a_window_that_has_reset_is_left_out_and_the_rest_are_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earlier = [subscription(window(16, minutes_to_reset=10), window(54, 3 * 24 * 60))]

    pi = await ask_slow(monkeypatch, earlier, later=11 * 60)

    [account] = accounts_of(pi)
    assert account.limits == [window(54, 3 * 24 * 60)]
    assert account.limits_error is None
    assert account.limits_checked_at == READ_AT


async def test_when_every_window_has_reset_the_account_says_the_device_was_too_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pi = await ask_slow(monkeypatch, [subscription(window(16, 5), window(54, 20))], later=21 * 60)

    [account] = accounts_of(pi)

    assert account.limits is None
    assert account.limits_error == SLOW_AGENT


async def test_a_window_that_names_no_reset_time_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    pi = await ask_slow(monkeypatch, [subscription(window(16, None))], later=25 * 60)

    [account] = accounts_of(pi)

    assert account.limits == [window(16, None)]
    assert account.limits_error is None


# ----------------------------------------- accounts that have no email, or no quota


async def test_an_account_with_no_email_is_never_given_windows_from_an_earlier_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pi = await ask_slow(monkeypatch, [subscription(window(16), email=None)], later=60)

    [account] = accounts_of(pi)

    assert account.limits is None, "nothing says whose windows they were"
    assert account.limits_error == SLOW_AGENT


async def test_one_account_never_gets_the_windows_another_of_its_vendor_last_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    monkeypatch.setattr(detection, "now_ms", Clock().now_ms)
    install(monkeypatch, everyone("1.0", pi=[subscription(window(16), email="one@example.com")]))
    await detect_all(WITH_QUOTA)
    install(monkeypatch, everyone("2.0", pi=[subscription(email="two@example.com")]))
    await detect_all()
    gate = slow(monkeypatch)

    pi = next(info for info in await detect_all(WITH_QUOTA) if info.agent == "pi")

    [account] = accounts_of(pi)
    assert account.email == "two@example.com"
    assert account.limits is None, "the other account's windows are not this one's"
    assert account.limits_error == SLOW_AGENT
    gate.set()
    await settle()


async def test_an_account_whose_vendor_exposes_no_windows_stays_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = AgentAccount(provider="xai", method="account", email=EMAIL)

    grok = await ask_slow(monkeypatch, [plain], agent="grok")

    assert accounts_of(grok) == [plain]


async def test_a_key_is_never_given_a_line_about_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    key = AgentAccount(provider="openai", method="api_key")

    pi = await ask_slow(monkeypatch, [subscription(window(16)), key])

    assert accounts_of(pi)[1] == key


async def test_an_earlier_error_is_replaced_by_the_slow_agents_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    erring = AgentAccount(
        provider="anthropic",
        method="account",
        email=EMAIL,
        limits_error="signed-in token expired; open Claude Code once to refresh it",
        limits_checked_at=READ_AT,
    )

    pi = await ask_slow(monkeypatch, [erring])

    [account] = accounts_of(pi)
    assert account.limits is None and account.limits_error == SLOW_AGENT


NEVER_ANSWERED = [
    ("claude", "anthropic", SLOW_AGENT),
    ("pi", "anthropic", SLOW_AGENT),
    ("codex", "openai", SLOW_AGENT),
    ("pi", "openai", None),
    ("pi", "xai", None),
    ("grok", "xai", None),
    ("claude", "openai", None),
]


@pytest.mark.parametrize(("agent", "provider", "line"), NEVER_ANSWERED)
async def test_a_request_for_quota_nothing_ever_answered_names_only_accounts_that_carry_quota(
    monkeypatch: pytest.MonkeyPatch, agent: str, provider: str, line: str | None
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0", **{agent: [subscription(provider=provider, email=None)]}))
    await detect_all()
    gate = slow(monkeypatch, agent)

    info = next(info for info in await detect_all(WITH_QUOTA) if info.agent == agent)

    [account] = accounts_of(info)
    assert account.limits is None
    assert account.limits_error == line
    assert (account.limits_checked_at is not None) == (line is not None)
    gate.set()
    await settle()


async def test_a_slow_agent_asked_for_a_plain_detection_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0", pi=[subscription(window(16))]))
    await detect_all(WITH_QUOTA)
    gate = slow(monkeypatch)
    waiting = asyncio.ensure_future(detect_all())
    await asyncio.sleep(0.2)

    assert not waiting.done(), "nobody is waiting for it, so it waits for the agent"
    gate.set()
    pi = next(info for info in await waiting if info.agent == "pi")

    assert pi.version == "2.0"


# ----------------------------------------------------------- what is remembered


async def test_a_plain_detection_in_between_does_not_take_the_windows_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    clock = Clock()
    monkeypatch.setattr(detection, "now_ms", clock.now_ms)
    install(monkeypatch, everyone("1.0", pi=[subscription(window(16))]))
    await detect_all(WITH_QUOTA)
    clock.advance(600)
    await detect_all()
    clock.advance(600)
    gate = slow(monkeypatch)

    pi = next(info for info in await detect_all(WITH_QUOTA) if info.agent == "pi")

    [account] = accounts_of(pi)
    assert account.limits == [window(16)]
    assert account.limits_checked_at == READ_AT
    gate.set()
    await settle()


async def test_what_is_kept_for_a_plain_request_never_carries_quota_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, everyone("1.0", pi=[subscription(window(16))]))

    await detect_all(WITH_QUOTA)

    kept = registry.detections.last_good["pi"].accounts
    assert kept is not None and kept[0].limits is None and kept[0].limits_checked_at is None
    quota = registry.detections.last_quota["pi"].accounts
    assert quota is not None and quota[0].limits == [window(16)]


async def test_a_plain_detection_leaves_what_a_quota_request_last_answered_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, everyone("1.0", pi=[subscription(window(16))]))
    await detect_all(WITH_QUOTA)
    kept = registry.detections.last_quota["pi"]

    install(monkeypatch, everyone("2.0", pi=[subscription(window(99))]))
    await detect_all()

    assert registry.detections.last_quota["pi"] is kept


async def test_the_windows_a_slow_agent_brings_late_are_what_it_is_shown_with_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    install(monkeypatch, everyone("1.0", pi=[subscription(window(16))]))
    await detect_all(WITH_QUOTA)
    gate = asyncio.Event()
    install(
        monkeypatch,
        {**everyone("2.0"), "pi": after(gate, reporting("pi", "2.0", [subscription(window(77))]))},
    )
    await detect_all(WITH_QUOTA)

    gate.set()
    await settle()

    quota = registry.detections.last_quota["pi"].accounts
    assert quota is not None and quota[0].limits == [window(77)]


# --------------------------------- the windows an account last had read, answer or no answer

RATE_LIMITED = "api.anthropic.com is limiting quota reads (HTTP 429); try again in 30 s"
KEPT_FOR = [("pi", "anthropic"), ("codex", "openai"), ("claude", "anthropic")]


def erring(
    error: str = RATE_LIMITED, email: str | None = EMAIL, provider: str = "anthropic"
) -> AgentAccount:
    """An account as an answer that carries only an error for it reports it."""
    return AgentAccount(provider=provider, method="account", email=email, limits_error=error)


async def answer_then_ask_slow(
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    answers: Sequence[tuple[float, list[AgentAccount]]],
    later: float,
) -> AgentInfo:
    """`agent` answers quota requests with each of `answers` at its time, and later is too slow."""
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    clock = Clock()
    monkeypatch.setattr(detection, "now_ms", clock.now_ms)
    for seconds, accounts in answers:
        clock.advance(seconds - (clock.now - 1_000))
        install(monkeypatch, everyone("1.0", **{agent: accounts}))
        await detect_all(WITH_QUOTA)
    clock.advance(later - (clock.now - 1_000))
    gate = slow(monkeypatch, agent)

    agents = await detect_all(WITH_QUOTA)

    gate.set()
    await settle()
    return next(info for info in agents if info.agent == agent)


@pytest.mark.parametrize(("agent", "provider"), KEPT_FOR)
async def test_an_answer_that_only_has_an_error_does_not_take_the_windows_away(
    monkeypatch: pytest.MonkeyPatch, agent: str, provider: str
) -> None:
    answers = [
        (0, [subscription(window(16), provider=provider)]),
        (600, [erring(provider=provider)]),
    ]

    info = await answer_then_ask_slow(monkeypatch, agent, answers, later=1_200)

    [account] = accounts_of(info)
    assert account.limits == [window(16)], "ten minutes old, whatever the answer after it said"
    assert account.limits_error is None
    assert account.limits_checked_at == READ_AT


async def test_the_windows_an_error_leaves_are_judged_by_the_time_they_were_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [(0, [subscription(window(16))]), (20 * 60, [erring()])]

    info = await answer_then_ask_slow(monkeypatch, "pi", answers, later=31 * 60)

    [account] = accounts_of(info)
    assert account.limits is None, "an error 11 minutes ago does not make them 11 minutes old"
    assert account.limits_error == SLOW_AGENT


async def test_one_error_after_another_still_leaves_the_windows_that_were_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [(0, [subscription(window(16))]), (300, [erring()]), (600, [erring("another error")])]

    info = await answer_then_ask_slow(monkeypatch, "pi", answers, later=900)

    [account] = accounts_of(info)
    assert account.limits == [window(16)]


async def test_an_answer_that_says_there_are_none_replaces_the_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    none = AgentAccount(
        provider="openai",
        method="account",
        email=EMAIL,
        limits=[],
        limits_checked_at=READ_AT + 600_000,
    )
    answers = [(0, [subscription(window(16), provider="openai")]), (600, [none])]

    info = await answer_then_ask_slow(monkeypatch, "codex", answers, later=1_200)

    [account] = accounts_of(info)
    assert account.limits is None, "the vendor said there are none, whatever it said before"
    assert account.limits_error is None


async def test_an_answer_that_has_no_way_to_read_replaces_the_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plain = AgentAccount(provider="anthropic", method="account", email=EMAIL)
    answers = [(0, [subscription(window(16))]), (600, [plain])]

    info = await answer_then_ask_slow(monkeypatch, "pi", answers, later=1_200)

    assert accounts_of(info) == [plain]


async def test_an_error_after_windows_for_an_account_with_no_email_keeps_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [(0, [subscription(window(16), email=None)]), (600, [erring(email=None)])]

    info = await answer_then_ask_slow(monkeypatch, "pi", answers, later=1_200)

    [account] = accounts_of(info)
    assert account.limits is None and account.limits_error == SLOW_AGENT


# ------------------------------------------------ truthful empty lists, and twins


@pytest.mark.parametrize("email", [EMAIL, None])
async def test_an_account_that_truthfully_reports_no_windows_stays_as_it_is(
    monkeypatch: pytest.MonkeyPatch, email: str | None
) -> None:
    truthful = AgentAccount(
        provider="openai",
        method="account",
        email=email,
        limits=[],
        limits_checked_at=READ_AT,
    )

    codex = await ask_slow(monkeypatch, [truthful], agent="codex", later=60)

    assert accounts_of(codex) == [replace(truthful, limits=None, limits_checked_at=None)]


async def test_accounts_that_share_an_identity_are_kept_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = erring(email=None)
    truthful = AgentAccount(
        provider="anthropic", method="account", limits=[], limits_checked_at=READ_AT
    )

    pi = await ask_slow(monkeypatch, [broken, truthful], later=60)

    first, second = accounts_of(pi)
    assert first.limits_error == SLOW_AGENT, "the first of the two said there was an error"
    assert second.limits is None and second.limits_error is None, "the second truthfully said none"


async def test_a_person_who_signs_in_as_another_account_is_not_shown_the_old_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detection, "AGENT_TIMEOUT", 0.05)
    monkeypatch.setattr(detection, "now_ms", Clock().now_ms)
    install(
        monkeypatch,
        everyone("1.0", codex=[subscription(window(16), email="a@example.com", provider="openai")]),
    )
    await detect_all(WITH_QUOTA)
    install(
        monkeypatch, everyone("2.0", codex=[subscription(email="b@example.com", provider="openai")])
    )
    await detect_all()
    gate = slow(monkeypatch, "codex")

    codex = next(info for info in await detect_all(WITH_QUOTA) if info.agent == "codex")

    [account] = accounts_of(codex)
    assert account.email == "b@example.com"
    assert account.limits is None
    assert account.limits_error == SLOW_AGENT
    gate.set()
    await settle()


# ---------------------------------------------------------------- `remember`, by itself


def info(*accounts: AgentAccount, agent: str = "codex") -> AgentInfo:
    return AgentInfo(agent=agent, available=True, accounts=list(accounts))


def test_remembering_the_first_answer_is_that_answer() -> None:
    first = info(subscription(window(16)))

    assert remember(None, first) is first


def test_an_answer_with_no_accounts_is_remembered_as_it_is() -> None:
    nothing = AgentInfo(agent="codex", available=False)

    assert remember(info(subscription(window(16))), nothing) is nothing


def test_windows_read_later_replace_windows_read_earlier() -> None:
    kept = remember(info(subscription(window(16))), info(subscription(window(77))))

    assert kept.accounts is not None and kept.accounts[0].limits == [window(77)]


def test_an_error_leaves_the_windows_of_the_account_it_is_about_and_no_other() -> None:
    other = "other@example.com"
    before = info(subscription(window(16)), subscription(window(33), email=other))
    now = info(subscription(window(18)), erring(email=other))

    kept = remember(before, now).accounts

    assert kept is not None
    assert [account.limits for account in kept] == [[window(18)], [window(33)]]
