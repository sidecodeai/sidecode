"""A62 through the real gateway: board requests are the gateway's, and boards leave with their
session, their device and their account. Remote control around them is unchanged."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rc_gateway.app import create_app
from rc_gateway.board import mount_board
from rc_gateway.board.policy import OperatorPolicy
from rc_gateway.frames import FORWARDED_TYPES
from rc_gateway.state import GatewayState

from ..conftest import (
    PASSWORD,
    add_member,
    device_hello,
    drain_until,
    enroll_device,
    session_summary,
    sign_in,
)
from .fakes import FakeModel

SESSION = "ad2c9abb-4a1e-470a-835c-228778fc17f0"
GET_ID = "61e50c72-d2bc-4a64-be32-62fdd1ba4635"


@pytest.fixture
def boards(state: GatewayState, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A gateway that keeps boards, on a model that is a fake."""
    monkeypatch.delenv("BOARD_MODEL", raising=False)
    app = create_app(state)
    mount_board(app, state, policy=OperatorPolicy(FakeModel()))
    with TestClient(app) as client:
        yield client


def auth_of(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {sign_in(client, 'admin', PASSWORD)}"}


def board_on(client: TestClient, auth: dict[str, str]) -> None:
    response = client.patch("/api/preferences", json={"task_board": True}, headers=auth)
    assert response.status_code == 200, response.text


def rows(tmp_path: Path) -> list[str]:
    path = tmp_path / "boards.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(path) as connection:
        return [str(row[0]) for row in connection.execute("SELECT session_id FROM boards")]


def eventually(check: Any, seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "never happened"
        time.sleep(0.01)


def board_get(app: Any, session_id: str = SESSION, identifier: str = GET_ID) -> dict[str, Any]:
    app.send_json({"type": "board.get", "id": identifier, "session_id": session_id})
    frame: dict[str, Any] = drain_until(app, "reply")
    return frame


def test_board_requests_are_the_gateways_and_never_reach_a_device(
    boards: TestClient, tmp_path: Path
) -> None:
    assert not {"board.get", "board.refresh", "board.updated"} & FORWARDED_TYPES
    auth = auth_of(boards)
    board_on(boards, auth)
    enrolled = enroll_device(boards, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with boards.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(sessions=[session_summary(SESSION, enrolled["device_id"])]))
        drain_until(device, "preferences")
        with boards.websocket_connect("/ws/app", headers=auth) as app:
            hello = drain_until(app, "hello")
            assert hello["board"] == {"enabled": True}
            reply = board_get(app)
            assert reply["ok"] is True
            assert reply["result"] == {
                "board": None,
                "status": {"state": "current", "reason": None},
            }
            app.send_json(
                {
                    "type": "board.refresh",
                    "id": "a6d0c2f7-31e9-4856-8541-d5ece2b4a9c3",
                    "session_id": SESSION,
                    "rebuild": False,
                }
            )
            assert drain_until(app, "reply")["result"]["status"]["state"] == "current"
            # The next thing the device hears is the app's own request, not a board frame.
            app.send_json({"type": "session.commands", "id": "c", "session_id": SESSION})
            assert device.receive_json()["type"] == "session.commands"
    assert rows(tmp_path) == [SESSION]
    assert boards.get("/api/config", headers=auth).json()["board"] == {"enabled": True}


def test_a_gateway_without_boards_answers_unsupported(
    client: TestClient, auth: dict[str, str]
) -> None:
    assert client.get("/api/config", headers=auth).json()["board"] == {"enabled": False}
    with client.websocket_connect("/ws/app", headers=auth) as app:
        assert drain_until(app, "hello")["board"] == {"enabled": False}
        reply = board_get(app)
    assert reply["ok"] is False and reply["error"]["code"] == "unsupported"


def test_another_accounts_session_is_not_found(boards: TestClient) -> None:
    auth = auth_of(boards)
    enrolled = enroll_device(boards, auth)
    member = add_member(boards, auth, "grace")
    board_on(boards, member)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with boards.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(sessions=[session_summary(SESSION, enrolled["device_id"])]))
        drain_until(device, "preferences")
        with boards.websocket_connect("/ws/app", headers=member) as app:
            drain_until(app, "hello")
            mine = board_get(app)
            unknown = board_get(app, "00000000-0000-4000-8000-000000000000", "b")
    assert mine["error"]["code"] == unknown["error"]["code"] == "not_found"


@pytest.mark.parametrize("deleted", ["session", "device", "account"])
def test_boards_leave_with_their_session_device_and_account(
    boards: TestClient, tmp_path: Path, deleted: str
) -> None:
    admin = auth_of(boards)
    member = add_member(boards, admin, "grace")
    board_on(boards, member)
    enrolled = enroll_device(boards, member)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with boards.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(sessions=[session_summary(SESSION, enrolled["device_id"])]))
        drain_until(device, "preferences")
        with boards.websocket_connect("/ws/app", headers=member) as app:
            drain_until(app, "hello")
            assert board_get(app)["ok"] is True
            assert rows(tmp_path) == [SESSION]
            if deleted == "session":
                device.send_json({"type": "session.removed", "session_id": SESSION})
                drain_until(app, "session.removed")
    if deleted == "device":
        assert (
            boards.delete(f"/api/devices/{enrolled['device_id']}", headers=member).status_code
            == 200
        )
    if deleted == "account":
        assert boards.delete("/api/users/grace", headers=admin).status_code == 200
    eventually(lambda: rows(tmp_path) == [])


def test_subscribe_and_history_answers_are_unchanged_with_boards(boards: TestClient) -> None:
    auth = auth_of(boards)
    board_on(boards, auth)
    enrolled = enroll_device(boards, auth)
    headers = {"Authorization": f"Bearer {enrolled['device_token']}"}
    with boards.websocket_connect("/ws/device", headers=headers) as device:
        device.send_json(device_hello(sessions=[session_summary(SESSION, enrolled["device_id"])]))
        drain_until(device, "preferences")
        with boards.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            board_get(app)
            app.send_json({"type": "session.subscribe", "id": "s", "session_id": SESSION})
            subscribed = drain_until(app, "reply")
            app.send_json(
                {"type": "session.history", "id": "h", "session_id": SESSION, "brief": True}
            )
            asked = drain_until(device, "session.history")
            assert asked["brief"] is True and asked["from"] != "gateway"
            device.send_json(
                {
                    "type": "reply",
                    "id": "h",
                    "from": asked["from"],
                    "ok": True,
                    "result": {"events": [], "has_more": False},
                }
            )
            history = drain_until(app, "reply")
    assert set(subscribed["result"]) == {"session", "events", "resync"}
    assert history == {
        "type": "reply",
        "id": "h",
        "ok": True,
        "result": {"events": [], "has_more": False},
    }
