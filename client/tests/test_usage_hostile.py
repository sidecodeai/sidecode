"""Whatever the vendor, or a proxy in the way, sends back never takes the reply down (A33).

A header or a body is somebody else's text. A date the parser cannot hold, a body nested past the
depth the parser allows and a number too large to be a percentage are lines of why or windows that
are skipped, never an exception out of `device.agents`.
"""

from __future__ import annotations

import pytest

from rc_client.agents import anthropic_usage, usage_fetch
from rc_client.agents.claude import account as claude_account
from rc_client.models import AgentAccount
from tests.usage_helpers import Reply, ok, refusal, serve, sign_in_claude

JSON = {"content-type": "application/json"}
LATER = "api.anthropic.com is limiting quota reads (HTTP 429); try again later"
MALFORMED_WAITS = [
    "Wed, 21 Oct 99999999999999999999 07:28:00 GMT",
    "Wed, 21 Oct 2015 07:28:00 +99999999999999999999",
    "Wed, 99999999999999999999 Oct 2015 07:28:00 GMT",
    "Wed, 21 Oct 2015 07:28:00 +9999999",
    "99999999999999999999",
    "soon",
    "-5",
    "1e9",
    "²",
    "",
    " ",
]


async def read() -> AgentAccount:
    [account] = await claude_account.detect(limits=True)
    return account


@pytest.mark.parametrize("wait", MALFORMED_WAITS)
async def test_a_retry_after_that_cannot_be_read_names_no_wait(
    monkeypatch: pytest.MonkeyPatch, wait: str
) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(429, "rate_limit_error", retry_after=wait))

    account = await read()

    assert account.limits_error == LATER


async def test_a_retry_after_far_in_the_future_is_taken_for_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(429, retry_after="Thu, 31 Dec 9999 23:59:59 GMT"))

    account = await read()

    assert account.limits_error == (
        "api.anthropic.com is limiting quota reads (HTTP 429); try again in 24 h"
    )


async def test_a_retry_after_in_the_past_names_no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(429, retry_after="Wed, 21 Oct 2015 07:28:00 GMT"))

    account = await read()

    assert account.limits_error == LATER


async def test_a_body_nested_past_the_depth_the_parser_allows_is_one_line_of_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, Reply(200, text="[" * 100_000, headers=JSON))

    account = await read()

    assert account.limits is None
    assert account.limits_error == usage_fetch.UNREADABLE


async def test_such_a_body_does_not_hide_what_the_status_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, Reply(503, text='{"error":' * 100_000, headers=JSON))

    account = await read()

    assert account.limits_error == "api.anthropic.com had a server error (HTTP 503)"


TOO_LARGE = "1" + "0" * 400


@pytest.mark.parametrize(
    "body",
    [
        '{"limits": [{"kind": "session", "percent": ' + TOO_LARGE + "}]}",
        '{"five_hour": {"utilization": ' + TOO_LARGE + "}}",
    ],
)
async def test_a_percentage_too_large_to_be_a_number_is_no_window(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    sign_in_claude()
    serve(monkeypatch, Reply(200, text=body, headers=JSON))

    account = await read()

    assert account.limits is None
    assert account.limits_error == anthropic_usage.NO_WINDOWS


async def test_one_window_too_large_to_be_a_number_does_not_cost_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    body = (
        '{"limits": [{"kind": "session", "percent": 12}, '
        '{"kind": "weekly_all", "percent": ' + TOO_LARGE + "}, "
        '{"kind": "weekly_scoped", "percent": Infinity}, '
        '{"kind": "weekly_scoped", "percent": NaN}]}'
    )
    serve(monkeypatch, Reply(200, text=body, headers=JSON))

    account = await read()

    assert account.limits is not None
    assert [(limit.window_minutes, limit.used_percent) for limit in account.limits] == [
        (300, 12),
        (10080, 100),
        (10080, 0),
    ]


async def test_a_number_and_a_date_that_do_not_fit_leave_the_rest_of_the_row_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    rows = [{"kind": "session", "percent": 12, "resets_at": "99999-99-99T00:00:00+00:00"}]
    serve(monkeypatch, ok({"limits": rows}))

    account = await read()

    assert account.limits is not None
    assert [(limit.used_percent, limit.resets_at) for limit in account.limits] == [(12, None)]


async def test_reading_the_answer_can_fail_without_failing_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, ok())

    def broken(*arguments: object) -> object:
        raise ArithmeticError("a part of the answer nobody foresaw")

    monkeypatch.setattr(usage_fetch, "_answered", broken)

    account = await read()

    assert account.provider == "anthropic" and account.limits is None
    assert account.limits_error == "could not read the Anthropic quota: ArithmeticError"
