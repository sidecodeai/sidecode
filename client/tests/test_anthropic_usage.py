"""What the device makes of Anthropic's usage endpoint, answer by answer (A33).

Every test sends the request the device really builds, through `httpx.MockTransport`, so the
headers, the deadline, the proxy, the parsing and the way each failure is told all run for real.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from email.utils import formatdate
from typing import Any

import httpx
import pytest

from rc_client.agents import anthropic_usage, usage_fetch
from rc_client.agents.claude import account as claude_account
from rc_client.agents.claude import plugin as claude_plugin
from rc_client.agents.claude import runtime as claude_runtime
from rc_client.agents.pi import account as pi_account
from rc_client.agents.pi import catalog as pi_catalog
from rc_client.agents.pi import plugin as pi_plugin
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.agents.pi.catalog import PiCatalog
from rc_client.agents.registry import DetectContext
from rc_client.config import Config, ensure_dirs
from rc_client.daemon import Daemon
from rc_client.logging_setup import JsonFormatter
from rc_client.models import AgentAccount, now_ms
from tests.usage_helpers import (
    HOUR_MS,
    TOKEN,
    USAGE_BODY,
    USAGE_WINDOWS,
    claude_credential,
    failing,
    keep_time,
    ok,
    page,
    refusal,
    serve,
    serve_with,
    sign_in_claude,
    write_json,
)

PROXY = "http://proxy.example:3128"
LOGGER = "rc_client.anthropic_usage"
EXPIRED_MS = 1_000


async def read(proxy: str = "") -> AgentAccount:
    [account] = await claude_account.detect(limits=True, proxy=proxy)
    return account


def usage_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == "anthropic usage read"]


# ----------------------------------------------------------- what a 200 comes to


async def test_the_request_is_a_get_of_the_usage_endpoint_with_the_token_and_the_beta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    served = serve(monkeypatch, ok())

    await read()

    [request] = served.requests
    assert (request.method, str(request.url)) == (
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
    )
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["accept"] == "application/json"


async def test_known_rows_become_windows_stamped_with_when_they_were_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, ok())
    before = now_ms()

    account = await read()

    assert account.limits is not None
    assert [limit.to_dict() for limit in account.limits] == USAGE_WINDOWS
    assert account.limits_error is None
    assert account.limits_checked_at is not None and account.limits_checked_at >= before


async def test_rows_this_device_does_not_know_fall_back_to_the_older_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    unknown = [{"kind": "monthly_credits", "percent": 12, "resets_at": None}]
    serve(monkeypatch, ok({**USAGE_BODY, "limits": unknown}))

    account = await read()

    assert account.limits is not None
    assert [limit.to_dict() for limit in account.limits] == USAGE_WINDOWS[:2]
    assert account.limits_error is None


@pytest.mark.parametrize(
    "body",
    [
        {"limits": [{"kind": "monthly_credits", "percent": 12}], "five_hour": None},
        {"limits": []},
        {},
    ],
)
async def test_a_body_with_no_window_it_recognises_says_so_instead_of_drawing_nothing(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    sign_in_claude()
    serve(monkeypatch, ok(body))

    account = await read()

    assert account.limits is None
    assert account.limits_error == anthropic_usage.NO_WINDOWS
    assert account.limits_checked_at is not None


@pytest.mark.parametrize(
    "reply", [page("<html>Sign in to the Wi-Fi</html>"), ok([1, 2]), ok("text")]
)
async def test_a_200_that_is_not_a_usage_body_is_one_line_of_why(
    monkeypatch: pytest.MonkeyPatch, reply: Any
) -> None:
    sign_in_claude()
    serve(monkeypatch, reply)

    account = await read()

    assert account.limits is None
    assert account.limits_error == "api.anthropic.com returned a usage body this device cannot read"


# -------------------------------------------------- a token the vendor refuses


def rotating(*tokens: str) -> claude_account.KeychainReader:
    """A Keychain holding a different token at each read, and failing on a read too many."""
    items = iter(json.dumps(claude_credential(now_ms() + HOUR_MS, token=token)) for token in tokens)

    async def keychain() -> str:
        return next(items)

    return keychain


def accepts(token: str) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {token}":
            return ok().response()
        return refusal(401, "authentication_error").response()

    return handler


async def test_a_401_re_reads_the_keychain_once_and_retries_with_the_token_it_finds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = serve_with(monkeypatch, accepts("token-after-rotation"))

    [account] = await claude_account.detect(
        limits=True, keychain=rotating("token-before-rotation", "token-after-rotation")
    )

    assert served.tokens == ["token-before-rotation", "token-after-rotation"]
    assert account.limits is not None
    assert [limit.to_dict() for limit in account.limits] == USAGE_WINDOWS
    assert account.limits_error is None


async def test_a_401_the_retry_meets_again_is_reported_and_never_tried_a_third_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = serve(monkeypatch, refusal(401, "authentication_error"))

    [account] = await claude_account.detect(
        limits=True, keychain=rotating("token-before-rotation", "token-after-rotation")
    )

    assert served.tokens == ["token-before-rotation", "token-after-rotation"]
    assert account.limits is None
    assert account.limits_error == claude_account.REJECTED


async def test_a_401_with_the_same_token_in_the_keychain_is_not_sent_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = serve(monkeypatch, refusal(401, "authentication_error"))

    [account] = await claude_account.detect(limits=True, keychain=rotating("same", "same"))

    assert served.tokens == ["same"]
    assert account.limits_error == claude_account.REJECTED


async def test_a_rotated_token_that_has_already_expired_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = serve(monkeypatch, refusal(401, "authentication_error"))
    items = iter(
        [
            json.dumps(claude_credential(now_ms() + HOUR_MS, token="old")),
            json.dumps(claude_credential(now_ms() - EXPIRED_MS, token="new")),
        ]
    )

    async def keychain() -> str:
        return next(items)

    [account] = await claude_account.detect(limits=True, keychain=keychain)

    assert served.tokens == ["old"]
    assert account.limits_error == claude_account.REJECTED


async def test_a_pi_sign_in_refused_by_the_vendor_is_told_to_sign_in_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(
        pi_account.auth_file(),
        {"anthropic": {"type": "oauth", "access": TOKEN, "expires": now_ms() + HOUR_MS}},
    )
    serve(monkeypatch, refusal(401, "authentication_error"))

    [account] = await pi_account.detect(limits=True)

    assert account.limits_error == anthropic_usage.REJECTED


async def test_a_403_says_the_sign_in_may_not_read_the_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(403, "permission_error"))

    account = await read()

    assert account.limits is None
    assert account.limits_error == "this sign-in is not allowed to read the quota (HTTP 403)"


# ---------------------------------------------------- a vendor that is limiting reads


@pytest.mark.parametrize(
    ("wait", "words"),
    [("30", "30 s"), ("90", "90 s"), ("120", "2 min"), ("5400", "90 min"), ("10800", "3 h")],
)
async def test_a_429_names_the_wait_the_vendor_asked_for(
    monkeypatch: pytest.MonkeyPatch, wait: str, words: str
) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(429, "rate_limit_error", retry_after=wait))

    account = await read()

    assert (
        account.limits_error
        == f"api.anthropic.com is limiting quota reads (HTTP 429); try again in {words}"
    )


@pytest.mark.parametrize("wait", [None, "soon", "0", "-5"])
async def test_a_429_with_no_usable_wait_says_to_try_again_later(
    monkeypatch: pytest.MonkeyPatch, wait: str | None
) -> None:
    sign_in_claude()
    headers = {} if wait is None else {"retry_after": wait}
    serve(monkeypatch, refusal(429, "rate_limit_error", **headers))

    account = await read()

    assert (
        account.limits_error
        == "api.anthropic.com is limiting quota reads (HTTP 429); try again later"
    )


async def test_a_wait_given_as_a_date_is_the_time_from_now_until_that_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(429, retry_after=formatdate(time.time() + 300, usegmt=True)))

    account = await read()

    assert (
        account.limits_error
        == "api.anthropic.com is limiting quota reads (HTTP 429); try again in 5 min"
    )


# ------------------------------------------------------- one request where one will do


async def test_a_second_ask_inside_a_minute_is_answered_from_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    served = serve(monkeypatch, ok())
    first = await read()

    clock.advance(59)
    second = await read()
    assert len(served.requests) == 1
    assert second.limits == first.limits
    assert second.limits_checked_at == first.limits_checked_at

    clock.advance(2)
    await read()
    assert len(served.requests) == 2


async def test_asks_made_together_share_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in_claude()
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return ok().response()

    served = serve_with(monkeypatch, handler)
    asks = [asyncio.ensure_future(read()) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert len(served.requests) == 1, "the other two must be waiting on the first"

    gate.set()
    accounts = await asyncio.gather(*asks)

    assert len(served.requests) == 1
    assert all(account.limits == accounts[0].limits for account in accounts)
    assert accounts[0].limits is not None


async def test_a_reader_that_stops_waiting_does_not_cancel_the_request_the_others_are_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return ok().response()

    served = serve_with(monkeypatch, handler)
    leaving = asyncio.ensure_future(read())
    staying = asyncio.ensure_future(read())
    await asyncio.sleep(0.05)

    leaving.cancel()
    gate.set()
    account = await staying

    assert leaving.cancelled()
    assert len(served.requests) == 1
    assert account.limits is not None


async def test_a_failed_read_is_not_held_so_the_next_ask_tries_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    served = serve(monkeypatch, failing(httpx.ReadTimeout("slow")), ok())

    first = await read()
    second = await read()

    assert first.limits_error == usage_fetch.SLOW
    assert second.limits is not None and second.limits_error is None
    assert len(served.requests) == 2


async def test_a_429_holds_the_next_read_off_for_as_long_as_the_vendor_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    served = serve(monkeypatch, refusal(429, "rate_limit_error", retry_after="120"), ok())

    first = await read()
    clock.advance(100)
    second = await read()
    assert len(served.requests) == 1
    assert second.limits_error == first.limits_error

    clock.advance(21)
    third = await read()
    assert len(served.requests) == 2
    assert third.limits is not None and third.limits_error is None


async def test_a_429_that_names_no_wait_is_held_for_a_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    served = serve(monkeypatch, refusal(429, "rate_limit_error"), ok())

    await read()
    clock.advance(59)
    await read()
    assert len(served.requests) == 1

    clock.advance(2)
    await read()
    assert len(served.requests) == 2


async def test_nothing_the_device_keeps_between_reads_holds_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude(email="me@example.com")
    serve(monkeypatch, ok())

    await read()

    assert TOKEN not in repr(vars(anthropic_usage.memory))
    assert TOKEN not in repr(claude_account.signed_in)
    assert "test-refresh-token" not in repr(vars(anthropic_usage.memory))


async def test_a_429_is_never_held_for_longer_than_ten_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    clock = keep_time(monkeypatch)
    served = serve(monkeypatch, refusal(429, "rate_limit_error", retry_after="3600"), ok())

    await read()
    clock.advance(599)
    await read()
    assert len(served.requests) == 1

    clock.advance(2)
    await read()
    assert len(served.requests) == 2


async def test_what_was_held_is_dropped_once_it_has_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok())
    for token in ("first", "second", "third"):
        sign_in_claude(token=token)
        await read()
        clock.advance(61)
    sign_in_claude(token="fourth")

    await read()

    assert len(anthropic_usage.memory._held) == 1, "only the answer that is still held stays"


@pytest.mark.parametrize(
    "reply",
    [
        refusal(403, "permission_error"),
        refusal(429, "rate_limit_error", retry_after="30"),
        refusal(500, "api_error"),
        failing(httpx.ReadTimeout("slow")),
    ],
)
async def test_a_refusal_that_is_not_a_401_never_reads_the_keychain_again(
    monkeypatch: pytest.MonkeyPatch, reply: Any
) -> None:
    reads: list[str] = []
    credential = json.dumps(claude_credential(now_ms() + HOUR_MS))

    async def keychain() -> str:
        reads.append("read")
        return credential

    served = serve(monkeypatch, reply)

    await claude_account.detect(limits=True, keychain=keychain)

    assert reads == ["read"], "only a 401 may mean that the token was rotated"
    assert len(served.requests) == 1


# ---------------------------------------------------------------- how it is dialled


async def test_the_read_has_one_deadline_in_all(monkeypatch: pytest.MonkeyPatch) -> None:
    sign_in_claude()
    monkeypatch.setattr(usage_fetch, "DEADLINE", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ok().response()

    served = serve_with(monkeypatch, slow)
    started = time.monotonic()

    account = await read()

    assert time.monotonic() - started < 2
    assert account.limits_error == usage_fetch.SLOW
    assert served.client["timeout"] == 0.05


async def test_the_deadline_is_five_seconds() -> None:
    assert usage_fetch.DEADLINE == 5.0


async def test_a_direct_dial_names_no_proxy_and_never_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    served = serve(monkeypatch, ok())

    await read()

    assert served.client == {"timeout": usage_fetch.DEADLINE, "trust_env": False, "proxy": None}


async def test_the_configured_proxy_is_passed_to_the_request_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    served = serve(monkeypatch, ok())

    await read(proxy=PROXY)

    assert served.client == {"timeout": usage_fetch.DEADLINE, "trust_env": False, "proxy": PROXY}


async def test_a_pi_sign_in_dials_through_the_proxy_too(monkeypatch: pytest.MonkeyPatch) -> None:
    write_json(
        pi_account.auth_file(),
        {"anthropic": {"type": "oauth", "access": TOKEN, "expires": now_ms() + HOUR_MS}},
    )
    served = serve(monkeypatch, ok())

    await pi_account.detect(limits=True, proxy=PROXY)

    assert served.client["proxy"] == PROXY


async def test_each_plugin_hands_the_context_proxy_to_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    write_json(
        pi_account.auth_file(),
        {"anthropic": {"type": "oauth", "access": "pi-token", "expires": now_ms() + HOUR_MS}},
    )
    served = serve(monkeypatch, ok())

    async def version(path: str) -> str:
        return "1.0.0"

    async def catalogue(binary: str | None) -> PiCatalog:
        return PiCatalog()

    monkeypatch.setattr(claude_runtime, "resolve_binary", lambda: "/nowhere/claude")
    monkeypatch.setattr(claude_runtime, "probe_version", version)
    monkeypatch.setattr(pi_runtime, "resolve_binary", lambda: "/nowhere/pi")
    monkeypatch.setattr(pi_runtime, "probe_version", version)
    monkeypatch.setattr(pi_catalog, "load", catalogue)

    context = DetectContext(limits=True, proxy=PROXY)
    await claude_plugin.detect(context)
    seen_by_claude = served.client
    await pi_plugin.detect(context)

    assert seen_by_claude["proxy"] == PROXY
    assert served.client["proxy"] == PROXY
    assert served.tokens == [TOKEN, "pi-token"]


async def test_the_daemon_hands_detection_the_proxy_it_was_enrolled_with() -> None:
    ensure_dirs()
    daemon = Daemon(
        Config(
            gateway_origin="http://127.0.0.1:8787",
            device_id="dev-1",
            device_token="tok-1",
            name="test-device",
            proxy=PROXY,
        )
    )
    try:
        assert daemon._detect_context(limits=True).proxy == PROXY
        assert daemon._detect_context().proxy == PROXY
    finally:
        daemon.registry.close()


# ---------------------------------------------------------------- what the log says


async def test_each_read_leaves_one_info_line_with_its_status_wait_type_and_duration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in_claude()
    serve(monkeypatch, refusal(429, "rate_limit_error", retry_after="30"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await read()

    [record] = usage_lines(caplog)
    line = json.loads(JsonFormatter().format(record))
    assert record.levelno == logging.INFO
    assert isinstance(line.pop("duration_ms"), int)
    assert isinstance(line.pop("ts"), int)
    assert line == {
        "level": "info",
        "logger": LOGGER,
        "msg": "anthropic usage read",
        "status": 429,
        "error_type": "rate_limit_error",
        "retry_after": 30,
        "content_type": "application/json",
    }


async def test_a_good_read_logs_only_what_it_has(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in_claude()
    serve(monkeypatch, ok())

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await read()

    [record] = usage_lines(caplog)
    context = record.__dict__["context"]
    assert sorted(context) == ["content_type", "duration_ms", "status"]
    assert (context["status"], context["content_type"]) == (200, "application/json")


async def test_a_read_that_gets_no_answer_logs_the_class_of_the_failure_and_no_status(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in_claude()
    serve(monkeypatch, failing(httpx.ConnectTimeout("slow")))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await read()

    [record] = usage_lines(caplog)
    context = record.__dict__["context"]
    assert sorted(context) == ["duration_ms", "failure"]
    assert context["failure"] == "ConnectTimeout"


async def test_an_answer_held_in_memory_leaves_no_line_of_its_own(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sign_in_claude()
    serve(monkeypatch, ok())

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await read()
        await read()

    assert len(usage_lines(caplog)) == 1


async def test_no_line_holds_the_token_a_header_or_the_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secrets = [
        TOKEN,
        "test-refresh-token",
        "a sentence the log must not repeat",
        "Bearer",
        "oauth-2025",
    ]
    outcomes = [
        ok(),
        refusal(401, "authentication_error"),
        refusal(403, "permission_error"),
        refusal(429, "rate_limit_error", retry_after="30"),
        refusal(503, "overloaded_error"),
        failing(httpx.ReadTimeout("slow")),
        page("<html>portal</html>"),
    ]
    told: list[str | None] = []
    clock = keep_time(monkeypatch)
    sign_in_claude()
    serve(monkeypatch, *outcomes)

    with caplog.at_level(logging.DEBUG):
        for _ in outcomes:
            clock.advance(601)
            told.append((await read()).limits_error)

    assert len(usage_lines(caplog)) >= len(outcomes)
    everything = caplog.text + json.dumps(
        [record.__dict__ for record in caplog.records], default=str
    )
    for secret in secrets:
        assert secret not in everything
        assert all(secret not in (line or "") for line in told)


# ------------------------------------------------------- what it does not change


async def test_a_token_that_has_expired_is_still_never_sent_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json(claude_account.credentials_file(), claude_credential(now_ms() - EXPIRED_MS))
    served = serve(monkeypatch, ok())

    account = await read()

    assert served.requests == []
    assert account.limits_error == claude_account.EXPIRED


async def test_a_sign_in_with_no_token_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    credential = claude_credential(now_ms() + HOUR_MS)
    credential["claudeAiOauth"]["accessToken"] = ""
    write_json(claude_account.credentials_file(), credential)
    served = serve(monkeypatch, ok())

    account = await read()

    assert served.requests == []
    assert account.limits_error == anthropic_usage.NO_TOKEN


async def test_an_unexpected_failure_costs_the_read_and_not_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_in_claude()
    serve(monkeypatch, failing(ValueError("no good")))

    account = await read()

    assert account.provider == "anthropic" and account.method == "account"
    assert account.limits is None
    assert account.limits_error == "could not read the Anthropic quota: ValueError"
