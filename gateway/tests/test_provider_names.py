"""AI provider names (A65): read from the environment, reported to the apps, and a new task board
provider asking every account again.

All of it holds on a self-hosted gateway, which may also leave every name unset.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import build_state, create_app
from rc_gateway.board.store import STORE_FILE, BoardStore
from rc_gateway.config import ConfigError, load_config
from rc_gateway.preference_store import PreferenceStore
from rc_gateway.provider_names import MAX_CHARS, VARIABLES, ProviderNames, parse

from .conftest import (
    PASSWORD,
    PROVIDER_NAMES,
    FakePolisher,
    FakeTranscriber,
    drain_until,
    make_config,
    sign_in,
)

#: Characters that would hide, break or reorder part of the sentence a name is shown in: NUL, tab,
#: line feed, escape, delete, next line, zero-width space, right-to-left override, line separator
#: and the byte order mark.
INVISIBLE = [
    chr(code) for code in (0x00, 0x09, 0x0A, 0x1B, 0x7F, 0x85, 0x200B, 0x202E, 0x2028, 0xFEFF)
]
DEVICE = "d1d1d1d1-0000-4000-8000-000000000001"
#: A board for each account: admin and dana have their task board on; erin turned hers off while
#: no board service ran, which left her board behind.
BOARDS = {
    "admin": "b0b0b0b0-0000-4000-8000-000000000001",
    "dana": "b0b0b0b0-0000-4000-8000-000000000002",
    "erin": "b0b0b0b0-0000-4000-8000-000000000003",
}
#: Whose task board is on before a start.
ON = {"admin": True, "dana": True, "erin": False}


def headers(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}


# --- the names and the configuration --------------------------------------------------------------


def test_a_name_is_trimmed_and_an_unset_one_is_empty() -> None:
    assert parse("STT_PROVIDER_NAME", "  OpenAI  ") == "OpenAI"
    assert parse("STT_PROVIDER_NAME", "") == ""
    assert parse("STT_PROVIDER_NAME", "   ") == ""
    assert parse("BOARD_PROVIDER_NAME", "Example Speech") == "Example Speech"
    # Any script is plain text.
    assert parse("BOARD_PROVIDER_NAME", "示例语音服务") == "示例语音服务"


def test_a_name_may_be_forty_characters_long_and_no_longer() -> None:
    longest = "x" * MAX_CHARS
    assert parse("POLISH_PROVIDER_NAME", longest) == longest
    with pytest.raises(ValueError, match="POLISH_PROVIDER_NAME is longer than 40 characters"):
        parse("POLISH_PROVIDER_NAME", longest + "x")


@pytest.mark.parametrize("character", INVISIBLE)
def test_a_name_with_a_control_or_invisible_character_is_refused(character: str) -> None:
    with pytest.raises(ValueError, match="LIVE_PROVIDER_NAME must be plain text"):
        parse("LIVE_PROVIDER_NAME", f"Open{character}AI")


def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **values: str) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://rc.example.com")
    monkeypatch.setenv("RC_PASSWORD", "hunter2hunter2")
    monkeypatch.delenv("WEB_PUSH_CONTACT", raising=False)
    for name in VARIABLES.values():
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_the_configuration_reads_the_four_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environment(monkeypatch, tmp_path)
    assert load_config(load_env_file=False).provider_names == ProviderNames()
    environment(
        monkeypatch,
        tmp_path,
        STT_PROVIDER_NAME=" OpenAI ",
        POLISH_PROVIDER_NAME="OpenAI",
        BOARD_PROVIDER_NAME="Board Co",
        LIVE_PROVIDER_NAME="Voice Co",
    )
    assert load_config(load_env_file=False).provider_names == ProviderNames(
        stt="OpenAI", polish="OpenAI", board="Board Co", live="Voice Co"
    )


@pytest.mark.parametrize("variable", list(VARIABLES.values()))
def test_the_gateway_does_not_start_with_a_name_an_app_could_not_show(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str
) -> None:
    for value in ("x" * (MAX_CHARS + 1), f"Open{chr(0x0A)}AI"):
        environment(monkeypatch, tmp_path, **{variable: value})
        with pytest.raises(ConfigError, match=variable):
            load_config(load_env_file=False)


# --- what the apps are told -----------------------------------------------------------------------


@pytest.fixture
def named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A self-hosted gateway that names every provider; its board is off (no ``BOARD_MODEL``)."""
    monkeypatch.delenv("BOARD_MODEL", raising=False)
    config = make_config(tmp_path, provider_names=PROVIDER_NAMES)
    state = build_state(config, transcriber=FakeTranscriber(), polisher=FakePolisher())
    with TestClient(create_app(state)) as client:
        yield client


def test_the_configuration_and_the_hello_name_each_provider(named: TestClient) -> None:
    auth = headers(named)
    config = named.get("/api/config", headers=auth).json()
    assert config["stt"] == {
        "enabled": True,
        "languages": ["auto"],
        "streaming": False,
        "provider_name": "Speech Co",
    }
    assert config["polish"] == {"enabled": True, "provider_name": "Polish Co"}
    # A feature that is off still names its provider: the name is the operator's to give.
    assert config["board"] == {"enabled": False, "provider_name": "Board Co"}
    with named.websocket_connect("/ws/app", headers=auth) as app:
        hello = drain_until(app, "hello")
    for feature in ("stt", "polish", "board"):
        assert hello[feature] == config[feature]


def test_an_unnamed_provider_is_left_out_and_still_offered_on_a_self_hosted_gateway(
    client: TestClient, auth: dict[str, str]
) -> None:
    config = client.get("/api/config", headers=auth).json()
    assert config["stt"] == {"enabled": True, "languages": ["auto"], "streaming": False}
    assert config["polish"] == {"enabled": True}
    assert "provider_name" not in config["board"]
    upload = client.post(
        "/api/stt/transcribe", files={"audio": ("a.wav", b"RIFF", "audio/wav")}, headers=auth
    )
    assert upload.status_code == 200, upload.text


@pytest.mark.parametrize(
    ("names", "named_as"),
    [(PROVIDER_NAMES, {"provider_name": "Voice Co"}), (ProviderNames(), {})],
)
def test_the_assistants_status_names_its_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    names: ProviderNames,
    named_as: dict[str, str],
) -> None:
    monkeypatch.setenv("LIVE_ASSISTANT_ENABLED", "true")
    monkeypatch.setenv("LIVE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("LIVE_ALLOWED_USERS", "admin")
    state = build_state(make_config(tmp_path, provider_names=names))
    with TestClient(create_app(state)) as client:
        status = client.get("/api/live/status", headers=headers(client)).json()
    assert status == {"enabled": True, "available": True, "reason": None, **named_as}


# --- a new task board provider --------------------------------------------------------------------


async def seed(data_dir: Path, *, recorded: str | None) -> None:
    """The accounts of ``ON`` with a board each, their permissions given under ``recorded``.

    The boards were opened just now, so nothing but a new provider deletes them.
    """
    preferences = PreferenceStore(data_dir / "preferences.sqlite3")
    boards = BoardStore(data_dir / STORE_FILE)
    now = int(time.time() * 1000)
    for username, session_id in BOARDS.items():
        await preferences.patch(username, {"task_board": ON[username]})
        await boards.create(session_id, DEVICE, username, viewed_at=now)
    if recorded is not None:
        await preferences.record_board_provider(recorded)


async def board_switches(data_dir: Path) -> dict[str, bool | None]:
    preferences = PreferenceStore(data_dir / "preferences.sqlite3")
    return {username: (await preferences.get(username)).task_board for username in BOARDS}


async def kept_boards(data_dir: Path) -> list[str]:
    boards = BoardStore(data_dir / STORE_FILE)
    return [
        session_id for session_id in BOARDS.values() if await boards.get(session_id) is not None
    ]


async def recorded_name(data_dir: Path) -> str:
    return await PreferenceStore(data_dir / "preferences.sqlite3").board_provider()


async def test_the_store_records_the_name_and_lists_the_accounts_whose_board_is_on(
    tmp_path: Path,
) -> None:
    store = PreferenceStore(tmp_path / "preferences.sqlite3")
    # A gateway that never recorded a name had none.
    assert await store.board_provider() == ""
    await store.patch("admin", {"task_board": True})
    await store.patch("dana", {"task_board": False})
    await store.patch("erin", {"language": "en"})
    assert await store.board_accounts() == ["admin"]
    await store.record_board_provider("Board Co")
    await store.record_board_provider("New Co")
    assert await store.board_provider() == "New Co"


def start(data_dir: Path, board_name: str) -> None:
    """Start a gateway on ``data_dir`` under ``board_name``, and stop it again."""
    config = make_config(data_dir, provider_names=ProviderNames(board=board_name))
    with TestClient(create_app(build_state(config))):
        pass


@pytest.mark.parametrize("board_model", ["board-model", ""])
@pytest.mark.parametrize(
    ("recorded", "current"),
    [("Old Co", "New Co"), (None, "Board Co"), ("", "Board Co"), ("Board Co", "")],
)
def test_a_new_board_provider_turns_every_task_board_off_and_deletes_the_boards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    board_model: str,
    recorded: str | None,
    current: str,
) -> None:
    # With the board kept its service deletes the boards; without it, they still go.
    monkeypatch.setenv("BOARD_MODEL", board_model)
    asyncio.run(seed(tmp_path, recorded=recorded))
    caplog.set_level(logging.INFO, logger="rc_gateway.board")
    start(tmp_path, current)
    assert asyncio.run(board_switches(tmp_path)) == dict.fromkeys(BOARDS, False)
    # Every board goes, the one erin's account left behind included.
    assert asyncio.run(kept_boards(tmp_path)) == []
    assert asyncio.run(recorded_name(tmp_path)) == current
    [line] = [
        record
        for record in caplog.records
        if record.getMessage() == "task boards turned off for a new provider"
    ]
    assert line.fields == {"accounts": 2}  # type: ignore[attr-defined]


@pytest.mark.parametrize(("recorded", "current"), [("Board Co", "Board Co"), (None, "")])
def test_the_same_board_provider_keeps_every_task_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: str | None, current: str
) -> None:
    monkeypatch.setenv("BOARD_MODEL", "board-model")
    asyncio.run(seed(tmp_path, recorded=recorded))
    start(tmp_path, current)
    assert asyncio.run(board_switches(tmp_path)) == ON
    assert asyncio.run(kept_boards(tmp_path)) == list(BOARDS.values())


def test_a_person_turns_the_board_on_again_under_the_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOARD_MODEL", "board-model")
    asyncio.run(seed(tmp_path, recorded="Old Co"))
    config = make_config(tmp_path, provider_names=ProviderNames(board="New Co"))
    with TestClient(create_app(build_state(config))) as client:
        auth = headers(client)
        stored = client.get("/api/preferences", headers=auth).json()["preferences"]
        assert stored["task_board"] is False
        on = client.patch("/api/preferences", json={"task_board": True}, headers=auth)
        assert on.json()["preferences"]["task_board"] is True
    # The next start under the same name leaves that choice alone.
    start(tmp_path, "New Co")
    assert asyncio.run(board_switches(tmp_path)) == {"admin": True, "dana": False, "erin": False}
