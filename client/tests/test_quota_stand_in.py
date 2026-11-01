"""When the vendor cannot answer just now, the windows read last stand in, within limits (A33).

A failure that passes by itself (the vendor limiting reads or refusing for now, a server error, a
timeout, no connection) is answered with the windows of the last good read, with no `limits_error`
and with the time they were read at, while that read is under thirty minutes old and for the
windows that have not reset since. A failure only the person can mend, and a body the device cannot
use, always say why instead.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from rc_client.agents import anthropic_usage, usage_fetch
from rc_client.agents.anthropic_usage import Bearer
from rc_client.agents.claude import account as claude_account
from rc_client.agents.pi import account as pi_account
from rc_client.models import AgentAccount, now_ms
from tests.usage_helpers import (
    HOUR_MS,
    READ_AT,
    TOKEN,
    USAGE_WINDOWS,
    claude_credential,
    failing,
    keep_time,
    ok,
    page,
    refusal,
    serve,
    sign_in_claude,
    usage_body_resetting,
    write_json,
)

LOGGER = "rc_client.anthropic_usage"
STOOD_IN = "anthropic usage served from an earlier read"
HALF_HOUR = 30 * 60

TRANSIENT = [
    (failing(httpx.ReadTimeout("slow")), "api.anthropic.com did not answer in time"),
    (failing(httpx.ConnectError("refused")), "could not reach api.anthropic.com: ConnectError"),
    (refusal(403, "permission_error"), "this sign-in is not allowed to read the quota (HTTP 403)"),
    (refusal(500, "api_error"), "api.anthropic.com had a server error (HTTP 500)"),
    (refusal(503, "overloaded_error"), "api.anthropic.com had a server error (HTTP 503)"),
    (refusal(529, "overloaded_error"), "api.anthropic.com had a server error (HTTP 529)"),
    (
        refusal(429, "rate_limit_error", retry_after="30"),
        "api.anthropic.com is limiting quota reads (HTTP 429); try again in 30 s",
    ),
]


async def read() -> AgentAccount:
    [account] = await claude_account.detect(limits=True)
    return account


def stood_in(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == STOOD_IN]


# ------------------------------------------------------------ what stands in


@pytest.mark.parametrize(("reply", "why"), TRANSIENT)
async def test_a_failure_that_passes_by_itself_serves_the_last_windows_with_no_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reply: Any, why: str
) -> None:
    sign_in_claude(email="me@example.com")
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), reply)
    first = await read()
    clock.advance(61)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        second = await read()

    assert second.limits == first.limits
    assert second.limits is not None
    assert [limit.to_dict() for limit in second.limits] == USAGE_WINDOWS
    assert second.limits_error is None
    assert second.limits_checked_at == first.limits_checked_at == READ_AT
    [line] = stood_in(caplog)
    assert line.levelno == logging.INFO
    assert line.__dict__["context"] == {"reason": why, "age_s": 61, "windows": 3}


@pytest.mark.parametrize(("reply", "why"), TRANSIENT)
async def test_the_same_failure_with_nothing_recent_enough_says_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reply: Any, why: str
) -> None:
    sign_in_claude(email="me@example.com")
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), reply)
    await read()
    clock.advance(HALF_HOUR)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        second = await read()

    assert second.limits is None
    assert second.limits_error == why
    assert second.limits_checked_at == clock.wall_ms
    assert stood_in(caplog) == []


async def test_windows_stand_in_up_to_but_not_including_thirty_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), failing(httpx.ReadTimeout("slow")))
    await read()

    clock.advance(HALF_HOUR - 1)
    inside = await read()
    clock.advance(1)
    outside = await read()

    assert inside.limits is not None and inside.limits_error is None
    assert inside.limits_checked_at == READ_AT
    assert outside.limits is None and outside.limits_error == usage_fetch.SLOW


async def test_the_same_windows_keep_standing_in_with_their_own_time_failure_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), failing(httpx.ReadTimeout("slow")))
    await read()

    stamps: list[int | None] = []
    for _ in range(3):
        clock.advance(300)
        stamps.append((await read()).limits_checked_at)

    assert stamps == [READ_AT, READ_AT, READ_AT]


async def test_a_good_read_replaces_the_windows_that_would_have_stood_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    newer = usage_body_resetting(session_minutes=120, week_minutes=4_000)
    serve(monkeypatch, ok(), ok(newer), failing(httpx.ReadTimeout("slow")))
    await read()
    clock.advance(120)
    await read()
    clock.advance(120)

    third = await read()

    assert third.limits is not None
    assert [limit.used_percent for limit in third.limits] == [40, 70]
    assert third.limits_checked_at == READ_AT + 120_000


async def test_a_held_429_is_judged_by_the_time_of_each_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), refusal(429, "rate_limit_error", retry_after="600"))
    await read()
    clock.advance(25 * 60)
    early = await read()

    clock.advance(5 * 60 + 50)
    late = await read()

    assert early.limits is not None and early.limits_error is None
    assert late.limits is None
    assert late.limits_error == (
        "api.anthropic.com is limiting quota reads (HTTP 429); try again in 10 min"
    )


# --------------------------------------------------- windows that have reset


async def test_a_window_that_has_reset_is_left_out_and_the_rest_stand_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(
        monkeypatch,
        ok(usage_body_resetting(session_minutes=10, week_minutes=3 * 24 * 60)),
        failing(httpx.ReadTimeout("slow")),
    )
    await read()
    clock.advance(11 * 60)

    second = await read()

    assert second.limits is not None
    assert [(limit.window_minutes, limit.used_percent) for limit in second.limits] == [(10080, 70)]
    assert second.limits_error is None
    assert second.limits_checked_at == READ_AT


async def test_a_window_stands_in_until_the_moment_it_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(
        monkeypatch,
        ok(usage_body_resetting(session_minutes=10, week_minutes=3 * 24 * 60)),
        failing(httpx.ReadTimeout("slow")),
    )
    await read()

    clock.advance(10 * 60 - 1)
    before = await read()
    clock.advance(1)
    at_reset = await read()

    assert before.limits is not None and len(before.limits) == 2
    assert at_reset.limits is not None and len(at_reset.limits) == 1


async def test_when_every_window_has_reset_the_reason_is_sent_instead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(
        monkeypatch,
        ok(usage_body_resetting(session_minutes=5, week_minutes=20)),
        failing(httpx.ReadTimeout("slow")),
    )
    await read()
    clock.advance(21 * 60)

    second = await read()

    assert second.limits is None
    assert second.limits_error == usage_fetch.SLOW
    assert second.limits_checked_at == clock.wall_ms


async def test_a_window_that_never_says_when_it_resets_stands_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(
        monkeypatch,
        ok(usage_body_resetting(session_minutes=None, week_minutes=None)),
        failing(httpx.ReadTimeout("slow")),
    )
    await read()
    clock.advance(25 * 60)

    second = await read()

    assert second.limits is not None and len(second.limits) == 2
    assert second.limits_error is None


# -------------------------------------------------- what never stands in


async def test_nothing_stands_in_for_a_token_that_has_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    served = serve(monkeypatch, ok())
    await read()
    write_json(claude_account.credentials_file(), claude_credential(clock.wall_ms - 1_000))
    clock.advance(61)

    second = await read()

    assert len(served.requests) == 1
    assert second.limits is None
    assert second.limits_error == claude_account.EXPIRED


async def test_nothing_stands_in_for_a_sign_in_with_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok())
    await read()
    credential = claude_credential(clock.wall_ms + 3_600_000)
    credential["claudeAiOauth"]["accessToken"] = ""
    write_json(claude_account.credentials_file(), credential)
    clock.advance(61)

    second = await read()

    assert second.limits is None
    assert second.limits_error == anthropic_usage.NO_TOKEN


async def test_nothing_stands_in_for_a_401_that_stays_a_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), refusal(401, "authentication_error"))
    await read()
    clock.advance(61)

    second = await read()

    assert second.limits is None
    assert second.limits_error == claude_account.REJECTED


UNUSABLE = [
    (ok({"limits": [{"kind": "monthly_credits", "percent": 12}]}), anthropic_usage.NO_WINDOWS),
    (ok({}), anthropic_usage.NO_WINDOWS),
    (page("<html>captive portal</html>"), usage_fetch.UNREADABLE),
    (ok([1, 2]), usage_fetch.UNREADABLE),
    (refusal(400, "invalid_request_error"), "api.anthropic.com answered HTTP 400"),
    (refusal(404, "not_found_error"), "api.anthropic.com answered HTTP 404"),
]


@pytest.mark.parametrize(("reply", "why"), UNUSABLE)
async def test_nothing_stands_in_for_what_the_device_cannot_use(
    monkeypatch: pytest.MonkeyPatch, reply: Any, why: str
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), reply)
    await read()
    clock.advance(61)

    second = await read()

    assert second.limits is None
    assert second.limits_error == why


async def test_windows_are_never_handed_to_another_account(monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in_claude(email="one@example.com")
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), refusal(503, "overloaded_error"))
    await read()
    clock.advance(61)
    sign_in_claude(token="the-other-accounts-token", email="two@example.com")

    second = await read()

    assert second.limits is None
    assert second.limits_error == "api.anthropic.com had a server error (HTTP 503)"


async def test_no_windows_stand_in_when_there_never_were_any(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, failing(httpx.ReadTimeout("slow")))

    account = await read()

    assert account.limits is None
    assert account.limits_error == usage_fetch.SLOW


# ------------------------------------------ whose windows they are


async def test_an_account_with_no_email_is_never_given_windows_from_an_earlier_reading(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in_claude(email=None)
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), failing(httpx.ReadTimeout("slow")))
    first = await read()
    clock.advance(61)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        second = await read()

    assert first.limits is not None, "a good read is still a good read"
    assert second.limits is None, "but nothing says whose windows they would be"
    assert second.limits_error == usage_fetch.SLOW
    assert stood_in(caplog) == []


async def test_two_accounts_with_no_email_never_share_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude(email=None)
    write_json(
        pi_account.auth_file(),
        {"anthropic": {"type": "oauth", "access": "pi-token", "expires": now_ms() + HOUR_MS}},
    )
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), failing(httpx.ReadTimeout("slow")))
    await read()
    clock.advance(61)

    [pi] = await pi_account.detect(limits=True)

    assert pi.limits is None
    assert pi.limits_error == usage_fetch.SLOW


async def test_the_same_email_under_another_agent_is_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), failing(httpx.ReadTimeout("slow")))
    one = AgentAccount(provider="anthropic", method="account", email="me@example.com")
    other = AgentAccount(provider="anthropic", method="account", email="me@example.com")
    await anthropic_usage.fill_limits("claude", one, Bearer(TOKEN))
    clock.advance(61)

    await anthropic_usage.fill_limits("pi", other, Bearer("another-token"))

    assert one.limits is not None
    assert other.limits is None
    assert other.limits_error == usage_fetch.SLOW


async def test_the_same_agent_and_email_is_the_same_account_whatever_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok(), failing(httpx.ReadTimeout("slow")))
    before = AgentAccount(provider="anthropic", method="account", email="me@example.com")
    after = AgentAccount(provider="anthropic", method="account", email="me@example.com")
    await anthropic_usage.fill_limits("claude", before, Bearer(TOKEN))
    clock.advance(61)

    await anthropic_usage.fill_limits("claude", after, Bearer("the-token-after-rotation"))

    assert after.limits == before.limits
    assert after.limits_error is None
