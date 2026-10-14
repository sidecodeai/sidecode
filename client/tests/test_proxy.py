"""What a proxy setting may be, how it is printed, and which dials it reaches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from rc_client import cli, pairing, update
from rc_client import proxy as proxy_module
from rc_client.config import Config, ensure_dirs, save_config
from rc_client.daemon import Daemon
from rc_client.errors import RcError
from rc_client.proxy import (
    httpx_options,
    normalise_proxy,
    redact_proxy,
    resolve_proxy,
    websocket_proxy,
)
from rc_client.update import self_update

GATEWAY = "https://rc.example.com"
PROXY = "http://proxy.example:3128"
PLAIN_PROXY = "http://plain.example:8080"


def environment(
    monkeypatch: pytest.MonkeyPatch, proxies: dict[str, str], bypassed: set[str] | None = None
) -> list[str]:
    """Stand in for this machine's proxy settings; returns the hosts asked about."""
    asked: list[str] = []
    bypass = bypassed or set()

    def is_bypassed(host: str) -> bool:
        asked.append(host)
        return host in bypass

    monkeypatch.setattr(proxy_module, "getproxies", lambda: dict(proxies))
    monkeypatch.setattr(proxy_module, "proxy_bypass", is_bypassed)
    return asked


# --------------------------------------------------------------- what is valid


def test_a_direct_dial_and_an_http_proxy_are_what_config_may_hold() -> None:
    assert normalise_proxy(None) == ""
    assert normalise_proxy("  ") == ""
    assert normalise_proxy(PROXY) == PROXY
    assert normalise_proxy(" https://proxy.example:3129 ") == "https://proxy.example:3129"


@pytest.mark.parametrize(
    "value",
    ["bogus", "env", "proxy.example:3128", "ftp://proxy.example", "http://", "http://:3128"],
)
def test_anything_else_is_a_clear_error(value: str) -> None:
    with pytest.raises(RcError) as caught:
        normalise_proxy(value)
    assert caught.value.code == "bad_request"
    assert "invalid proxy" in str(caught.value)


def test_a_socks_proxy_is_refused_by_naming_the_two_schemes_that_work() -> None:
    # Dialling SOCKS needs a package this client does not ship, so httpx and
    # websockets would both raise ImportError on every attempt instead.
    with pytest.raises(RcError) as caught:
        normalise_proxy("socks5://127.0.0.1:1080")
    assert "only http and https proxies are supported" in caught.value.message


def test_a_proxy_password_is_never_part_of_what_is_printed() -> None:
    assert redact_proxy("") == "direct"
    assert redact_proxy(PROXY) == PROXY
    assert redact_proxy("http://agent:s3cret@proxy.example:3128") == PROXY
    assert redact_proxy("socks5://agent:s3cret@proxy.example:1080") == "socks5://proxy.example:1080"
    assert "s3cret" not in redact_proxy("agent:s3cret@proxy.example:3128")


# ------------------------------------------------- `env`, resolved at enrolment


def test_env_takes_the_entry_for_the_gateway_this_device_dials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment(monkeypatch, {"http": PLAIN_PROXY, "https": PROXY})
    assert resolve_proxy("env", GATEWAY) == PROXY
    assert resolve_proxy("env", "http://rc.internal:8000") == PLAIN_PROXY


def test_env_is_matched_whatever_its_case(monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch, {"https": PROXY})
    assert resolve_proxy(" ENV ", GATEWAY) == PROXY


def test_env_is_direct_when_no_proxy_bypasses_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = environment(monkeypatch, {"https": PROXY}, bypassed={"rc.example.com"})
    assert resolve_proxy("env", GATEWAY) == ""
    assert asked == ["rc.example.com"]


def test_env_is_direct_when_this_machine_names_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch, {})
    assert resolve_proxy("env", GATEWAY) == ""


def test_a_url_and_a_direct_dial_are_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    environment(monkeypatch, {"https": PLAIN_PROXY})
    assert resolve_proxy(PROXY, GATEWAY) == PROXY
    assert resolve_proxy("", GATEWAY) == ""


# --------------------------------------------------------------- what is dialled


def test_a_direct_dial_ignores_the_environment() -> None:
    assert httpx_options("") == {"trust_env": False, "proxy": None}
    assert websocket_proxy("") is None


def test_a_url_names_one_proxy_and_still_ignores_the_environment() -> None:
    assert httpx_options(PROXY) == {"trust_env": False, "proxy": PROXY}
    assert websocket_proxy(PROXY) == PROXY


def test_the_gateway_link_dials_the_way_the_device_enrolled() -> None:
    config = Config(
        gateway_origin=GATEWAY,
        device_id="dev-1",
        device_token="tok-1",
        name="login-node",
        proxy=PROXY,
    )
    assert Daemon(config).link._proxy == PROXY


async def test_the_scan_pairing_client_dials_the_way_enrolment_will(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}
    real_init = httpx.AsyncClient.__init__

    def record(self: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        real_init(self, **kwargs)

    async def create_request(origin: str, client: httpx.AsyncClient) -> pairing.ClaimRequest:
        return pairing.ClaimRequest(token="tok-1", claim_url=f"{origin}/claim", expires_at=0)

    async def await_code(origin: str, token: str, client: httpx.AsyncClient, **kwargs: Any) -> str:
        return "RC-AAAA-BBBB"

    monkeypatch.setattr(httpx.AsyncClient, "__init__", record)
    monkeypatch.setattr(pairing, "create_request", create_request)
    monkeypatch.setattr(pairing, "await_code", await_code)
    assert await cli._claim_by_scanning(GATEWAY, PROXY) == "RC-AAAA-BBBB"
    assert seen == {"timeout": pairing.POLL_TIMEOUT, "trust_env": False, "proxy": PROXY}


async def test_the_update_download_dials_the_way_the_device_enrolled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    save_config(
        Config(
            gateway_origin=GATEWAY,
            device_id="dev-1",
            device_token="tok-1",
            name="login-node",
            proxy=PROXY,
        )
    )
    ensure_dirs()
    seen: dict[str, str] = {}

    async def download(origin: str, directory: Path, proxy: str) -> Path:
        seen.update(origin=origin, proxy=proxy)
        wheel = directory / "rc_client-0.0.0-py3-none-any.whl"
        wheel.write_bytes(b"not the build that was asked for")
        return wheel

    monkeypatch.setattr(update, "_download", download)
    assert await self_update("a" * 64) is False
    assert seen == {"origin": GATEWAY, "proxy": PROXY}
    assert "not the requested build" in capsys.readouterr().err
