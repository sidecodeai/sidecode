"""Amendment A23: pairing a host by having someone scan the code it prints."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from rc_client import pairing, qr
from rc_client.cli import EXIT_OK, main
from rc_client.config import load_config
from rc_client.errors import RcError
from rc_client.pairing import await_code, create_request

GATEWAY = "https://rc.example.com"
TOKEN = "7ZK3M9Q2X5H8B1V4N6P0R2T4W6"
CLAIM_URL = f"{GATEWAY}/pair#{TOKEN}"
CODE = "RC-7K42-QX9M"
EXPIRES = 1788945000000

Handler = Callable[[httpx.Request], httpx.Response]


async def gateway(handler: Handler) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield client


def minted(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"token": TOKEN, "expires_at": EXPIRES, "claim_url": CLAIM_URL})


async def test_a_host_asks_for_a_claim_token() -> None:
    async for client in gateway(minted):
        request = await create_request(GATEWAY, client)
        assert request.token == TOKEN
        assert request.claim_url == CLAIM_URL
        assert request.expires_at == EXPIRES


async def test_a_rate_limited_host_is_told_to_wait() -> None:
    async for client in gateway(lambda request: httpx.Response(429, json={})):
        with pytest.raises(RcError) as caught:
            await create_request(GATEWAY, client)
        assert caught.value.code == "conflict"


async def test_the_poll_waits_and_then_returns_the_claimed_code() -> None:
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        assert request.url.path == f"/api/pairing/requests/{TOKEN}"
        polls += 1
        if polls == 1:
            return httpx.Response(200, json={"status": "waiting"})
        return httpx.Response(200, json={"status": "claimed", "code": CODE, "expires_at": EXPIRES})

    async for client in gateway(handler):
        assert await await_code(GATEWAY, TOKEN, client, pause=0.0) == CODE
    assert polls == 2


async def test_an_expired_token_says_to_run_the_command_again() -> None:
    async for client in gateway(lambda request: httpx.Response(410, json={})):
        with pytest.raises(RcError) as caught:
            await await_code(GATEWAY, TOKEN, client, pause=0.0)
        assert caught.value.message == pairing.EXPIRED


async def test_a_forgotten_token_says_to_run_the_command_again() -> None:
    async for client in gateway(lambda request: httpx.Response(404, json={})):
        with pytest.raises(RcError) as caught:
            await await_code(GATEWAY, TOKEN, client, pause=0.0)
        assert caught.value.message == pairing.FORGOTTEN


async def test_the_poll_gives_up_once_the_token_can_no_longer_be_claimed() -> None:
    async for client in gateway(lambda request: httpx.Response(200, json={"status": "waiting"})):
        with pytest.raises(RcError) as caught:
            await await_code(GATEWAY, TOKEN, client, pause=0.0, total_wait=0.0)
        assert caught.value.code == "timeout"


# ------------------------------------------------------------------ the code


def test_the_qr_code_is_a_block_grid_a_terminal_can_hold() -> None:
    rows = qr.render(CLAIM_URL).splitlines()
    assert all(set(row) <= set("\xa0▀▄█") for row in rows)
    # 29 modules plus a one-module quiet zone, two module rows to a text row:
    # error correction L keeps a claim URL at this size, which fits any terminal.
    assert [len(row) for row in rows] == [31] * len(rows)
    assert len(rows) == 16


def test_the_same_url_always_renders_the_same_code() -> None:
    assert qr.render(CLAIM_URL) == qr.render(CLAIM_URL)
    assert qr.render(CLAIM_URL) != qr.render(f"{GATEWAY}/pair#OTHER")


# --------------------------------------------------------------- the command


def test_enroll_by_scanning_prints_the_code_and_enrols_with_it(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[str] = []

    async def post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        posted.append(url)
        request = httpx.Request("POST", url)
        if url.endswith("/api/pairing/requests"):
            return httpx.Response(
                200,
                json={"token": TOKEN, "expires_at": EXPIRES, "claim_url": CLAIM_URL},
                request=request,
            )
        assert kwargs["json"]["code"] == CODE
        return httpx.Response(
            200,
            json={
                "device_id": "dev-9",
                "device_token": "tok-9",
                "gateway_ws_url": "wss://rc.example.com/ws/device",
            },
            request=request,
        )

    async def get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "claimed", "code": CODE, "expires_at": EXPIRES},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert main(["enroll", "--gateway", GATEWAY, "--scan"]) == EXIT_OK

    output = capsys.readouterr().out
    assert CLAIM_URL in output
    assert "█" in output
    assert TOKEN not in output.replace(CLAIM_URL, "")
    assert posted == [f"{GATEWAY}/api/pairing/requests", f"{GATEWAY}/api/devices/enroll"]
    assert load_config().device_id == "dev-9"


def test_enroll_needs_a_code_or_a_scan() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["enroll", "--gateway", GATEWAY])
    assert caught.value.code == 2
