"""How Claude Code's credential is read from the Keychain, and what a failed read leaves (A33).

`security` is never run here: a fake takes its place, and a test that let the real one start
would be reading the Keychain of whoever runs the suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from rc_client.agents import anthropic_usage
from rc_client.agents.claude import account as claude_account
from rc_client.agents.claude import keychain
from rc_client.agents.claude.keychain import KeychainUnreadable
from rc_client.agents.claude.keychain import read_keychain as read_real_keychain
from rc_client.models import now_ms
from tests.usage_helpers import (
    HOUR_MS,
    READ_AT,
    TOKEN,
    USAGE_WINDOWS,
    claude_credential,
    keep_time,
    ok,
    refusal,
    serve,
    write_json,
)

LOGGER = "rc_client.claude.keychain"
EMAIL = "me@example.com"
ITEM_TEXT = "SECRET-KEYCHAIN-ITEM-TEXT"


class FakeProcess:
    """What `asyncio` hands back for `security`: it exits with `returncode`, or never answers."""

    def __init__(self, returncode: int, out: bytes, hangs: bool) -> None:
        self.returncode = returncode
        self.out = out
        self.hangs = hangs
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hangs:
            await asyncio.sleep(3600)
        return self.out, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class Security:
    """The stand-in for `security`: what it was asked, and the process it gave back."""

    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[Any, ...]] = []


def run_security(
    monkeypatch: pytest.MonkeyPatch,
    code: int = 0,
    out: bytes = b"",
    hangs: bool = False,
    refuses: Exception | None = None,
) -> Security:
    security = Security(FakeProcess(code, out, hangs))

    async def spawn(*argv: Any, **options: Any) -> FakeProcess:
        security.calls.append(argv)
        if refuses is not None:
            raise refuses
        return security.process

    monkeypatch.setattr(keychain, "IS_MACOS", True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return security


def lines(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


# ------------------------------------------------- no test reads a real Keychain


async def test_no_test_can_read_the_real_keychain_by_any_route() -> None:
    """The guard `conftest.py` installs, asserted once so it cannot rot."""
    assert keychain.read_keychain is not read_real_keychain, "replaced where it lives"
    used = vars(claude_account)["read_keychain"]
    assert used is keychain.read_keychain, "and where it is used"
    assert keychain.IS_MACOS is False, "and the platform says there is no Keychain to ask"
    assert await keychain.read_keychain() is None
    assert await used() is None


# ---------------------------------------------------------------- the three outcomes


async def test_the_item_is_read_through_security_by_its_service_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    security = run_security(monkeypatch, out=f"  {ITEM_TEXT}\n".encode())

    with caplog.at_level(logging.DEBUG):
        assert await read_real_keychain() == ITEM_TEXT

    assert security.calls == [
        ("security", "find-generic-password", "-s", "Claude Code-credentials", "-w")
    ]
    assert ITEM_TEXT not in caplog.text
    assert lines(caplog, LOGGER) == []


async def test_a_machine_with_no_keychain_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    security = run_security(monkeypatch)
    monkeypatch.setattr(keychain, "IS_MACOS", False)

    assert await read_real_keychain() is None
    assert security.calls == []


async def test_an_item_that_is_not_there_is_nothing_and_is_logged_by_class_and_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_security(monkeypatch, code=44, out=ITEM_TEXT.encode())

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await read_real_keychain() is None

    [record] = lines(caplog, LOGGER)
    assert record.levelno == logging.INFO
    assert record.getMessage() == "claude keychain read"
    assert record.__dict__["context"] == {"outcome": "missing", "exit": 44}
    assert ITEM_TEXT not in caplog.text


@pytest.mark.parametrize("code", [1, 36, 51, 128])
async def test_any_other_exit_is_an_item_that_cannot_be_read_right_now(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: int
) -> None:
    run_security(monkeypatch, code=code, out=ITEM_TEXT.encode())

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(KeychainUnreadable):
        await read_real_keychain()

    [record] = lines(caplog, LOGGER)
    assert record.levelno == logging.WARNING
    assert record.__dict__["context"] == {"outcome": "unreadable", "exit": code}
    assert ITEM_TEXT not in caplog.text


async def test_a_security_that_does_not_answer_in_time_is_killed_and_unreadable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(keychain, "KEYCHAIN_TIMEOUT", 0.05)
    security = run_security(monkeypatch, hangs=True)

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(KeychainUnreadable):
        await read_real_keychain()

    assert security.process.killed
    [record] = lines(caplog, LOGGER)
    assert record.__dict__["context"] == {"outcome": "timeout"}


async def test_a_security_that_cannot_start_is_unreadable_and_not_an_item_that_is_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run_security(monkeypatch, refuses=OSError("too many open files"))

    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(KeychainUnreadable):
        await read_real_keychain()

    [record] = lines(caplog, LOGGER)
    assert record.__dict__["context"] == {"outcome": "spawn_failed"}
    assert "too many open files" not in caplog.text


# --------------------------------------------- what the account makes of each outcome


def readers(*steps: str | Exception | None) -> claude_account.KeychainReader:
    """A Keychain that gives `steps` one read at a time: an item, nothing, or a failure."""
    remaining = iter(steps)

    async def reader() -> str | None:
        step = next(remaining)
        if isinstance(step, Exception):
            raise step
        return step

    return reader


def item(plan: str = "max", token: str = TOKEN) -> str:
    return json.dumps(claude_credential(now_ms() + HOUR_MS, plan, token))


def with_email() -> None:
    """Claude Code records the account's email in `~/.claude.json`, which tells it from another."""
    write_json(claude_account.config_file(), {"oauthAccount": {"emailAddress": EMAIL}})


BUSY = KeychainUnreadable("timeout")


async def test_an_item_that_cannot_be_read_now_is_not_a_person_who_signed_out() -> None:
    reader = readers(item("pro"), BUSY)

    [first] = await claude_account.detect(False, reader)
    [again] = await claude_account.detect(False, reader)

    assert first.to_dict() == {
        "provider": "anthropic",
        "method": "account",
        "plan": "pro",
        "tier": "Max 5x",
    }
    assert again.to_dict() == first.to_dict()


async def test_quota_asked_of_a_keychain_that_cannot_be_read_serves_the_windows_read_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_email()
    clock = keep_time(monkeypatch)
    served = serve(monkeypatch, ok())
    reader = readers(item(), BUSY)
    [first] = await claude_account.detect(True, reader)
    clock.advance(600)

    [again] = await claude_account.detect(True, reader)

    assert len(served.requests) == 1, "no token was read, so nothing may be sent"
    assert again.limits is not None
    assert [limit.to_dict() for limit in again.limits] == USAGE_WINDOWS
    assert again.limits_checked_at == first.limits_checked_at == READ_AT
    assert again.limits_error is None


async def test_windows_thirty_minutes_old_no_longer_stand_in_for_a_keychain_that_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_email()
    clock = keep_time(monkeypatch)
    serve(monkeypatch, ok())
    reader = readers(item(), BUSY)
    await claude_account.detect(True, reader)
    clock.advance(30 * 60)

    [again] = await claude_account.detect(True, reader)

    assert again.limits is None
    assert again.limits_error == claude_account.KEYCHAIN_BUSY
    assert again.limits_checked_at == clock.wall_ms


async def test_quota_withheld_with_no_windows_read_before_is_the_reason_alone() -> None:
    reader = readers(item(), BUSY)
    await claude_account.detect(False, reader)

    [again] = await claude_account.detect(True, reader)

    assert again.limits is None
    assert again.limits_error == claude_account.KEYCHAIN_BUSY
    assert again.limits_checked_at is not None


async def test_with_nothing_remembered_an_item_that_cannot_be_read_is_signed_in_nowhere() -> None:
    assert await claude_account.detect(True, readers(BUSY)) == []


async def test_a_read_that_works_again_replaces_what_was_remembered() -> None:
    reader = readers(item("pro"), BUSY, item("max"), BUSY)

    await claude_account.detect(False, reader)
    await claude_account.detect(False, reader)
    await claude_account.detect(False, reader)
    [last] = await claude_account.detect(False, reader)

    assert last.plan == "max"


async def test_an_item_that_is_missing_is_a_person_who_signed_out_and_is_forgotten() -> None:
    reader = readers(item(), None, BUSY)

    [first] = await claude_account.detect(False, reader)
    assert first.method == "account"
    assert await claude_account.detect(False, reader) == []
    assert await claude_account.detect(False, reader) == [], "a signed-out account must not return"


async def test_an_item_with_no_oauth_block_is_signed_out_logged_by_class_and_forgotten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reader = readers(item(), json.dumps({"mcpOAuth": {"server": {"accessToken": "other"}}}), BUSY)
    await claude_account.detect(False, reader)

    with caplog.at_level(logging.INFO, logger="rc_client.claude.account"):
        assert await claude_account.detect(False, reader) == []
    assert await claude_account.detect(False, reader) == []

    [record] = lines(caplog, "rc_client.claude.account")
    assert record.getMessage() == "claude credential read"
    assert record.__dict__["context"] == {"outcome": "no_oauth_block"}
    assert "other" not in caplog.text


async def test_the_account_it_remembers_holds_no_token() -> None:
    reader = readers(item(token="remembered-token"), BUSY)
    await claude_account.detect(False, reader)

    assert "remembered-token" not in repr(claude_account.signed_in)
    assert "remembered-token" not in repr(vars(anthropic_usage.memory))


async def test_a_token_that_may_have_rotated_but_cannot_be_re_read_is_not_sent_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = serve(monkeypatch, refusal(401, "authentication_error"))

    [account] = await claude_account.detect(True, readers(item(token="old"), BUSY))

    assert served.tokens == ["old"]
    assert account.limits_error == claude_account.REJECTED
