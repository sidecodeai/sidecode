"""The oldest iOS, Mac, Android and Windows apps the gateway works with (A31, A45, A46).

The four constants, the eight env values and the three bodies that carry them. The minimums move
separately, so every check covers each app and no app's settings may reach another app's entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.compat import (
    ANDROID_MINIMUM_APP_VERSION,
    IOS_MINIMUM_APP_VERSION,
    MACOS_MINIMUM_APP_VERSION,
    WINDOWS_MINIMUM_APP_VERSION,
    apps_view,
    is_release_version,
)
from rc_gateway.config import ConfigError, load_config

from .conftest import FIXTURE_DIR, drain_until, make_config

UPDATE_URL = "https://testflight.apple.com/join/EXAMPLE"
MAC_UPDATE_URL = "https://example.com/remote-control-mac"
#: Each app's release constant, by its key in `apps` and the prefix of its two env values.
MINIMUMS = {
    "ios": IOS_MINIMUM_APP_VERSION,
    "macos": MACOS_MINIMUM_APP_VERSION,
    "android": ANDROID_MINIMUM_APP_VERSION,
    "windows": WINDOWS_MINIMUM_APP_VERSION,
}
APP_VARIABLES = tuple(
    f"{app.upper()}_{suffix}" for app in MINIMUMS for suffix in ("MIN_APP_VERSION", "UPDATE_URL")
)


def fixture(*parts: str) -> Any:
    return json.loads(FIXTURE_DIR.joinpath(*parts).read_text(encoding="utf-8"))


def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **values: str) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)
    for name in APP_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def unconfigured_view() -> dict[str, dict[str, str]]:
    """`apps` of a gateway whose operator set nothing: every constant, no update URL."""
    return {app: {"minimum_version": minimum} for app, minimum in MINIMUMS.items()}


# --- J1 the constants and the eight env values --------------------------------------------


@pytest.mark.parametrize("constant", list(MINIMUMS.values()))
def test_the_release_constants_are_major_minor_patch_versions(constant: str) -> None:
    """The apps compare them component by component, and the schema accepts nothing else."""
    assert is_release_version(constant)


@pytest.mark.parametrize(
    "value", ["1", "1.2", "1.2.3.4", "v1.2.3", "1.2.3-beta", "latest", "1.2.x", "1.2.3\n"]
)
def test_only_three_numeric_components_count_as_a_version(value: str) -> None:
    assert not is_release_version(value)


def test_the_constants_are_the_defaults_and_the_env_overrides_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env(monkeypatch, tmp_path)
    unset = load_config(load_env_file=False)
    for app, minimum in MINIMUMS.items():
        assert getattr(unset, f"{app}_minimum_version") == minimum
        assert getattr(unset, f"{app}_update_url") == ""

    env(
        monkeypatch,
        tmp_path,
        IOS_MIN_APP_VERSION="2.3.4",
        IOS_UPDATE_URL=UPDATE_URL,
        MACOS_MIN_APP_VERSION="1.12.0",
        MACOS_UPDATE_URL=MAC_UPDATE_URL,
        ANDROID_MIN_APP_VERSION="1.13.0",
        ANDROID_UPDATE_URL="https://example.com/remote-control-android",
        WINDOWS_MIN_APP_VERSION="1.14.0",
        WINDOWS_UPDATE_URL="https://example.com/remote-control-windows",
    )
    overridden = load_config(load_env_file=False)
    assert overridden.ios_minimum_version == "2.3.4"
    assert overridden.ios_update_url == UPDATE_URL
    assert overridden.macos_minimum_version == "1.12.0"
    assert overridden.macos_update_url == MAC_UPDATE_URL
    assert overridden.android_minimum_version == "1.13.0"
    assert overridden.android_update_url == "https://example.com/remote-control-android"
    assert overridden.windows_minimum_version == "1.14.0"
    assert overridden.windows_update_url == "https://example.com/remote-control-windows"


@pytest.mark.parametrize("app", list(MINIMUMS))
def test_one_apps_variables_leave_the_other_apps_alone(
    app: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A45, A46: the minimums move separately, so no app falls back on another app's values."""
    prefix = app.upper()
    env(
        monkeypatch,
        tmp_path,
        **{
            f"{prefix}_MIN_APP_VERSION": "9.9.9",
            f"{prefix}_UPDATE_URL": "https://example.com/newer",
        },
    )
    config = load_config(load_env_file=False)
    for other, minimum in MINIMUMS.items():
        minimum_version = getattr(config, f"{other}_minimum_version")
        entry = (minimum_version, getattr(config, f"{other}_update_url"))
        if other == app:
            assert entry == ("9.9.9", "https://example.com/newer")
        else:
            assert entry == (minimum, "")


@pytest.mark.parametrize("name", [f"{app.upper()}_MIN_APP_VERSION" for app in MINIMUMS])
def test_a_minimum_that_is_not_a_version_stops_the_gateway(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env(monkeypatch, tmp_path, **{name: "1.2"})
    with pytest.raises(ConfigError) as refused:
        load_config(load_env_file=False)
    assert str(refused.value).startswith(f"{name} is not a major.minor.patch version")


@pytest.mark.parametrize("name", [f"{app.upper()}_UPDATE_URL" for app in MINIMUMS])
def test_the_update_url_must_be_https(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain-http link would fail the schema and reach the app as a dead button."""
    env(monkeypatch, tmp_path, **{name: "http://testflight.apple.com/join/EXAMPLE"})
    with pytest.raises(ConfigError) as refused:
        load_config(load_env_file=False)
    assert str(refused.value).startswith(f"{name} must be an https:// address")


def test_the_view_omits_an_update_url_that_was_not_configured(tmp_path: Path) -> None:
    assert apps_view(make_config(tmp_path)) == unconfigured_view()
    mac_url = make_config(tmp_path, macos_update_url=MAC_UPDATE_URL)
    assert apps_view(mac_url) == {
        **unconfigured_view(),
        "macos": {"minimum_version": MACOS_MINIMUM_APP_VERSION, "update_url": MAC_UPDATE_URL},
    }
    every_url = make_config(
        tmp_path,
        ios_minimum_version="2.0.0",
        ios_update_url=UPDATE_URL,
        macos_minimum_version="1.12.0",
        macos_update_url=MAC_UPDATE_URL,
        android_minimum_version="1.13.0",
        android_update_url="https://example.com/remote-control-android",
        windows_minimum_version="1.14.0",
        windows_update_url="https://example.com/remote-control-windows",
    )
    assert apps_view(every_url) == {
        "ios": {"minimum_version": "2.0.0", "update_url": UPDATE_URL},
        "macos": {"minimum_version": "1.12.0", "update_url": MAC_UPDATE_URL},
        "android": {
            "minimum_version": "1.13.0",
            "update_url": "https://example.com/remote-control-android",
        },
        "windows": {
            "minimum_version": "1.14.0",
            "update_url": "https://example.com/remote-control-windows",
        },
    }


# --- J2 what health, config and hello report ----------------------------------------------


def test_health_config_and_hello_all_carry_every_minimum(tmp_path: Path) -> None:
    """The three places an app can learn its minimum, whichever it sees first (8.16).

    The gateway is configured as the fixture is, so every body matches it value for value.
    """
    expected = fixture("http", "health.response.json")["apps"]
    config = make_config(
        tmp_path,
        ios_minimum_version=expected["ios"]["minimum_version"],
        ios_update_url=expected["ios"]["update_url"],
        macos_minimum_version=expected["macos"]["minimum_version"],
        android_minimum_version=expected["android"]["minimum_version"],
        windows_minimum_version=expected["windows"]["minimum_version"],
    )

    with TestClient(create_app(build_state(config))) as client:
        assert client.get("/api/health").json()["apps"] == expected
        token = client.post(
            "/api/login",
            json={"username": "admin", "password": config.password},
            headers={"Origin": config.public_origin},
        ).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/config", headers=auth).json()["apps"] == expected
        with client.websocket_connect("/ws/app", headers=auth) as app:
            assert drain_until(app, "hello")["apps"] == expected


def test_health_needs_no_credential_to_state_the_minimums(client: TestClient) -> None:
    """An app too old to sign in still learns why before it asks for a password."""
    assert client.get("/api/health").json()["apps"] == unconfigured_view()
