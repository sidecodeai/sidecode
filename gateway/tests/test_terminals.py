"""Terminal routing (amendment A38, §7.3).

A terminal is one person's view: the gateway forwards the five requests to the machine they name,
delivers `terminal.output` and `terminal.exited` to the single app connection the device addressed
and to nobody else, and asks the device to detach a terminal whose connection is gone — once,
however long the shell keeps printing.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from .conftest import (
    add_member,
    collect_until,
    device_hello,
    drain_until,
    enroll_device,
)

TERMINAL_ID = "2f8d4b6a-1c3e-4a75-9b0d-6e2f8c4a1d57"
OTHER_TERMINAL_ID = "3a9e5c7b-2d4f-4b86-ac1e-7f3a9d5b2e68"
#: Base64 of "$ ", the whole of what any of these tests puts through a terminal.
OUTPUT = "JCA="


def _device_headers(enrolled: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {enrolled['device_token']}"}


def _open_terminal(
    app: Any,
    device: Any,
    device_id: str,
    *,
    identifier: str = "open-1",
    terminal_id: str = TERMINAL_ID,
) -> str:
    """Open a terminal from one app socket and return the connection id the device was given."""
    app.send_json(
        {"type": "terminal.open", "id": identifier, "device_id": device_id, "cols": 80, "rows": 24}
    )
    forwarded = drain_until(device, "terminal.open")
    assert forwarded["device_id"] == device_id
    assert forwarded["cols"] == 80
    assert forwarded["rows"] == 24
    assert forwarded["from"]
    device.send_json(
        {
            "type": "reply",
            "id": identifier,
            "from": forwarded["from"],
            "ok": True,
            "result": {"terminal_id": terminal_id},
        }
    )
    reply = drain_until(app, "reply")
    assert reply["ok"] is True
    assert reply["result"] == {"terminal_id": terminal_id}
    return str(forwarded["from"])


def _attach_terminal(
    app: Any,
    device: Any,
    device_id: str,
    *,
    identifier: str = "attach-1",
    terminal_id: str = TERMINAL_ID,
) -> str:
    """Move a terminal to another socket of the same account and return that socket's id."""
    app.send_json(
        {
            "type": "terminal.attach",
            "id": identifier,
            "device_id": device_id,
            "terminal_id": terminal_id,
        }
    )
    forwarded = drain_until(device, "terminal.attach")
    device.send_json(
        {
            "type": "reply",
            "id": identifier,
            "from": forwarded["from"],
            "ok": True,
            "result": {"terminal_id": terminal_id, "cols": 80, "rows": 24, "scrollback": OUTPUT},
        }
    )
    reply = drain_until(app, "reply")
    assert reply["result"]["scrollback"] == OUTPUT
    return str(forwarded["from"])


def _output(to: str, *, seq: int = 1, terminal_id: str = TERMINAL_ID) -> dict[str, Any]:
    return {
        "type": "terminal.output",
        "terminal_id": terminal_id,
        "to": to,
        "seq": seq,
        "data": OUTPUT,
    }


def _marker(app: Any, device: Any, device_id: str, identifier: str) -> list[dict[str, Any]]:
    """Put one forwarded request through the device and return everything it arrived behind.

    The absence of a `terminal.detach` is what several of these tests assert, and a frame the
    gateway is known to send afterwards is the only way to wait for "nothing else came".
    """
    app.send_json(
        {
            "type": "terminal.input",
            "id": identifier,
            "device_id": device_id,
            "terminal_id": TERMINAL_ID,
            "data": OUTPUT,
        }
    )
    return collect_until(device, "terminal.input")


def _answer_detach(device: Any, frame: dict[str, Any]) -> None:
    device.send_json(
        {"type": "reply", "id": frame["id"], "from": "gateway", "ok": True, "result": {}}
    )


def test_output_reaches_the_holder_alone(client: TestClient, auth: dict[str, str]) -> None:
    """Two sockets of one account, one terminal: only the connection that opened it is fed."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with (
            client.websocket_connect("/ws/app", headers=auth) as holder,
            client.websocket_connect("/ws/app", headers=auth) as bystander,
        ):
            drain_until(holder, "hello")
            drain_until(bystander, "hello")
            holder_id = _open_terminal(holder, device, enrolled["device_id"])

            device.send_json(_output(holder_id))
            delivered = drain_until(holder, "terminal.output")
            assert delivered["terminal_id"] == TERMINAL_ID
            assert delivered["device_id"] == enrolled["device_id"]
            assert delivered["seq"] == 1
            assert delivered["data"] == OUTPUT
            # The device's addressing never leaves the gateway.
            assert "to" not in delivered

            # The account's other socket is fed nothing: a terminal is not broadcast.
            bystander.send_json(
                {"type": "device.dirs", "id": "dirs-1", "device_id": enrolled["device_id"]}
            )
            forwarded = drain_until(device, "device.dirs")
            device.send_json(
                {
                    "type": "reply",
                    "id": "dirs-1",
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {"path": "/", "parent": None, "entries": [], "recent": []},
                }
            )
            seen = collect_until(bystander, "reply")
            assert [item["type"] for item in seen if item["type"] == "terminal.output"] == []


def test_another_accounts_socket_never_receives_a_terminal(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A device may name any connection id; only its owner's sockets can be one (A24, A38)."""
    member = add_member(client, auth, "someone")
    mine = enroll_device(client, auth)
    theirs = enroll_device(client, member, name="their-laptop")
    with (
        client.websocket_connect("/ws/device", headers=_device_headers(mine)) as device,
        client.websocket_connect("/ws/device", headers=_device_headers(theirs)) as other_device,
    ):
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        other_device.send_json(device_hello(terminal=True))
        other_device.receive_json()
        with client.websocket_connect("/ws/app", headers=member) as their_app:
            drain_until(their_app, "hello")
            # Their own device tells them what their connection id is.
            their_id = _open_terminal(
                their_app, other_device, theirs["device_id"], identifier="open-theirs"
            )

            # My device now addresses their socket. It is refused and detached instead.
            device.send_json(_output(their_id, terminal_id=OTHER_TERMINAL_ID))
            detach = drain_until(device, "terminal.detach")
            assert detach["terminal_id"] == OTHER_TERMINAL_ID
            assert detach["from"] == "gateway"
            assert detach["device_id"] == mine["device_id"]
            _answer_detach(device, detach)

            their_app.send_json(
                {"type": "device.dirs", "id": "dirs-2", "device_id": theirs["device_id"]}
            )
            forwarded = drain_until(other_device, "device.dirs")
            other_device.send_json(
                {
                    "type": "reply",
                    "id": "dirs-2",
                    "from": forwarded["from"],
                    "ok": True,
                    "result": {"path": "/", "parent": None, "entries": [], "recent": []},
                }
            )
            seen = collect_until(their_app, "reply")
            assert [item["type"] for item in seen if item["type"] == "terminal.output"] == []


def test_a_terminal_for_a_connection_that_is_gone_is_detached_once(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A shell that keeps printing into nothing costs one request, not one per frame."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            _open_terminal(app, device, enrolled["device_id"])

            device.send_json(_output("no-such-connection", seq=1))
            detach = drain_until(device, "terminal.detach")
            assert detach["terminal_id"] == TERMINAL_ID
            _answer_detach(device, detach)

            device.send_json(_output("no-such-connection", seq=2))
            device.send_json(_output("no-such-connection", seq=3))
            behind = _marker(app, device, enrolled["device_id"], "input-1")
            assert [item["type"] for item in behind if item["type"] == "terminal.detach"] == []


def test_the_holders_socket_closing_detaches_its_terminals(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A38: the shell survives the socket, detached, for the device's ten minutes."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            _open_terminal(app, device, enrolled["device_id"])
        detach = drain_until(device, "terminal.detach")
        assert detach["terminal_id"] == TERMINAL_ID
        assert detach["from"] == "gateway"
        _answer_detach(device, detach)


def test_attach_from_a_second_socket_moves_the_terminal(
    client: TestClient, auth: dict[str, str]
) -> None:
    """The previous holder gets nothing more, and its socket closing detaches nothing."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as second:
            drain_until(second, "hello")
            with client.websocket_connect("/ws/app", headers=auth) as first:
                drain_until(first, "hello")
                _open_terminal(first, device, enrolled["device_id"])
                second_id = _attach_terminal(second, device, enrolled["device_id"])

                device.send_json(_output(second_id, seq=2))
                delivered = drain_until(second, "terminal.output")
                assert delivered["seq"] == 2
                assert delivered["device_id"] == enrolled["device_id"]
            # The socket that opened it no longer holds it, so its close detaches nothing.
            behind = _marker(second, device, enrolled["device_id"], "input-2")
            assert [item["type"] for item in behind if item["type"] == "terminal.detach"] == []
        detach = drain_until(device, "terminal.detach")
        assert detach["terminal_id"] == TERMINAL_ID
        _answer_detach(device, detach)


def test_exited_reaches_the_holder_and_frees_the_terminal(
    client: TestClient, auth: dict[str, str]
) -> None:
    """The shell ended: the holder is told, and nothing is detached when its socket closes."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as watcher:
            drain_until(watcher, "hello")
            with client.websocket_connect("/ws/app", headers=auth) as holder:
                drain_until(holder, "hello")
                holder_id = _open_terminal(holder, device, enrolled["device_id"])
                device.send_json(
                    {
                        "type": "terminal.exited",
                        "terminal_id": TERMINAL_ID,
                        "to": holder_id,
                        "code": 0,
                    }
                )
                exited = drain_until(holder, "terminal.exited")
                assert exited["code"] == 0
                assert exited["device_id"] == enrolled["device_id"]
                assert "to" not in exited
            behind = _marker(watcher, device, enrolled["device_id"], "input-3")
            assert [item["type"] for item in behind if item["type"] == "terminal.detach"] == []


def test_an_accepted_close_frees_the_terminal(client: TestClient, auth: dict[str, str]) -> None:
    """Closing a terminal is the other way it is forgotten, so no detach follows the socket."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as watcher:
            drain_until(watcher, "hello")
            with client.websocket_connect("/ws/app", headers=auth) as holder:
                drain_until(holder, "hello")
                _open_terminal(holder, device, enrolled["device_id"])
                holder.send_json(
                    {
                        "type": "terminal.close",
                        "id": "close-1",
                        "device_id": enrolled["device_id"],
                        "terminal_id": TERMINAL_ID,
                    }
                )
                forwarded = drain_until(device, "terminal.close")
                device.send_json(
                    {
                        "type": "reply",
                        "id": "close-1",
                        "from": forwarded["from"],
                        "ok": True,
                        "result": {},
                    }
                )
                assert drain_until(holder, "reply")["ok"] is True
            behind = _marker(watcher, device, enrolled["device_id"], "input-4")
            assert [item["type"] for item in behind if item["type"] == "terminal.detach"] == []


def test_terminal_requests_are_forwarded_to_the_device_they_name(
    client: TestClient, auth: dict[str, str]
) -> None:
    """All five travel by `device_id`, with `from` stamped, exactly as `device.mkdir` does."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            requests: list[dict[str, Any]] = [
                {"type": "terminal.open", "id": "f-1", "cols": 80, "rows": 24},
                {"type": "terminal.input", "id": "f-2", "terminal_id": TERMINAL_ID, "data": OUTPUT},
                {
                    "type": "terminal.resize",
                    "id": "f-3",
                    "terminal_id": TERMINAL_ID,
                    "cols": 100,
                    "rows": 32,
                },
                {"type": "terminal.attach", "id": "f-4", "terminal_id": TERMINAL_ID},
                {"type": "terminal.close", "id": "f-5", "terminal_id": TERMINAL_ID},
            ]
            for request in requests:
                app.send_json({**request, "device_id": enrolled["device_id"]})
                forwarded = drain_until(device, str(request["type"]))
                assert forwarded["id"] == request["id"]
                assert forwarded["device_id"] == enrolled["device_id"]
                assert forwarded["from"]


def test_a_terminal_on_another_accounts_device_is_unknown(
    client: TestClient, auth: dict[str, str]
) -> None:
    """A32-style probing is refused the same way a session on another account's device is."""
    member = add_member(client, auth, "someone")
    mine = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(mine)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=member) as their_app:
            drain_until(their_app, "hello")
            their_app.send_json(
                {
                    "type": "terminal.open",
                    "id": "probe-1",
                    "device_id": mine["device_id"],
                    "cols": 80,
                    "rows": 24,
                }
            )
            reply = drain_until(their_app, "reply")
            assert reply["ok"] is False
            assert reply["error"]["code"] == "not_found"


def test_hello_terminal_is_stored_and_reported(client: TestClient, auth: dict[str, str]) -> None:
    """`Device.terminal` comes from `hello` and reaches the list and the pushed update (A38)."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/app", headers=auth) as app:
        drain_until(app, "hello")
        with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
            device.send_json(device_hello(terminal=True))
            device.receive_json()
            updated = drain_until(app, "device.updated")
    assert updated["device"]["terminal"] is True
    listed = client.get("/api/devices", headers=auth).json()["devices"][0]
    assert listed["terminal"] is True
    with client.websocket_connect("/ws/app", headers=auth) as app:
        hello = drain_until(app, "hello")
    assert hello["devices"][0]["terminal"] is True


def test_a_client_that_never_said_carries_no_terminal_field(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Absent is not false: a client older than A38 must not read as a device without a shell."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello())
        device.receive_json()
        listed = client.get("/api/devices", headers=auth).json()["devices"][0]
    assert "terminal" not in listed


def test_an_agents_update_does_not_take_the_shell_away(
    client: TestClient, auth: dict[str, str]
) -> None:
    """Only `hello` says whether a device offers a terminal; `agents.updated` says nothing."""
    enrolled = enroll_device(client, auth)
    with client.websocket_connect("/ws/device", headers=_device_headers(enrolled)) as device:
        device.send_json(device_hello(terminal=True))
        device.receive_json()
        with client.websocket_connect("/ws/app", headers=auth) as app:
            drain_until(app, "hello")
            device.send_json({"type": "agents.updated", "agents": []})
            updated = drain_until(app, "device.updated")
    assert updated["device"]["terminal"] is True
