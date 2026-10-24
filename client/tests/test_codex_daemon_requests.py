"""Amendment A11: what the device answers when the shared daemon asks, and what it leaves alone.

The daemon sends a thread's request to every client subscribed to it, and the first
reply of any kind settles it. A client that answers what is not its to answer has
answered it for the terminal too.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rc_client.agents.codex.daemon.dialogs import ELICITATION_METHOD
from rc_client.agents.codex.daemon.session import CodexDaemonSession
from rc_client.errors import RcError
from tests.test_codex_daemon_sessions import THREAD, Harness, device_driven, settle, started
from tests.test_codex_daemon_sessions import harness as harness

DECLINED = {"action": "decline", "content": None, "_meta": None}
# Requests the apps have nothing to show for.
UNHANDLED: list[tuple[str, dict[str, Any]]] = [
    ("item/tool/call", {"threadId": THREAD, "tool": "lookup", "arguments": {}}),
    ("currentTime/read", {"threadId": THREAD}),
]


def elicitation() -> dict[str, Any]:
    """An MCP tool call asking to be approved, as Codex 0.163 puts it to its clients."""
    return {
        "threadId": THREAD,
        "turnId": "turn-1",
        "serverName": "docs",
        "mode": "form",
        "message": "Allow the docs server to search?",
        "requestedSchema": {"type": "object", "properties": {}},
    }


def runner_of(harness: Harness) -> CodexDaemonSession:
    runner = harness.hub.entry(THREAD).runner
    assert isinstance(runner, CodexDaemonSession)
    return runner


def watching(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The ids of the requests the device is done with, answered or not.

    Install it before the service starts: the client keeps the handler it was given.
    """
    done: list[Any] = []
    handle = harness.service._request

    async def request(request_id: Any, method: str, params: dict[str, Any]) -> Any:
        try:
            return await handle(request_id, method, params)
        finally:
            done.append(request_id)

    monkeypatch.setattr(harness.service, "_request", request)
    return done


async def running_here(harness: Harness) -> None:
    """A turn this device started, on a thread no terminal is in."""
    await device_driven(harness)
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    runner = runner_of(harness)
    assert runner.local_turn and not runner.terminal_holds


async def next_answer(harness: Harness) -> dict[str, Any]:
    return await asyncio.wait_for(harness.daemon.answers.get(), timeout=2)


async def declined_next(harness: Harness, request_id: int) -> None:
    """The next answer the daemon hears declines this request, so nothing was said before it."""
    assert await next_answer(harness) == {"jsonrpc": "2.0", "id": request_id, "result": DECLINED}
    assert harness.daemon.answers.empty()


# ------------------------------------------------------- what is not ours to answer


@pytest.mark.parametrize(("method", "params"), UNHANDLED)
async def test_an_unhandled_request_a_terminal_could_answer_gets_no_reply(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, method: str, params: dict[str, Any]
) -> None:
    done = watching(harness, monkeypatch)
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    assert runner_of(harness).terminal_holds

    await harness.daemon.ask(31, method, params)
    await settle(lambda: 31 in done)

    # Once the terminal has gone the next request is answered at once, so an
    # answer to this one would have come first.
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    await harness.daemon.ask(32, ELICITATION_METHOD, elicitation())
    await declined_next(harness, 32)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("item/commandExecution/requestApproval", {"threadId": "t-stranger", "command": "ls"}),
        ("item/tool/requestUserInput", {"threadId": "t-stranger", "questions": []}),
        ("account/chatgptAuthTokens/refresh", {"reason": "unauthorized"}),
    ],
)
async def test_a_request_for_a_thread_with_no_session_gets_no_reply(
    harness: Harness, method: str, params: dict[str, Any]
) -> None:
    await running_here(harness)
    await harness.daemon.ask(33, method, params)
    await harness.daemon.ask(34, ELICITATION_METHOD, elicitation())
    await declined_next(harness, 34)


# ------------------------------------------- what nobody else could answer


@pytest.mark.parametrize(("method", "params"), UNHANDLED)
async def test_an_unhandled_request_nobody_else_could_answer_is_refused(
    harness: Harness, method: str, params: dict[str, Any]
) -> None:
    """The turn is ours and no terminal is in the thread: an error ends the wait."""
    await running_here(harness)
    await harness.daemon.ask(35, method, params)
    assert await next_answer(harness) == {
        "jsonrpc": "2.0",
        "id": 35,
        "error": {"code": -32000, "message": f"unhandled codex request {method}"},
    }


async def test_an_approval_on_a_thread_with_no_terminal_is_put_to_the_apps(
    harness: Harness,
) -> None:
    """What the apps can show they show, whoever else could have answered it."""
    await running_here(harness)
    await harness.daemon.ask(
        36,
        "item/commandExecution/requestApproval",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "itemId": "exec-1",
            "command": "ls",
            "availableDecisions": ["accept", "cancel"],
        },
    )
    await settle(lambda: harness.events("approval"))
    card = harness.events("approval")[0]
    await harness.hub.approve(
        {"session_id": THREAD, "request_id": card["request_id"], "option_id": "allow"}
    )
    assert await next_answer(harness) == {
        "jsonrpc": "2.0",
        "id": 36,
        "result": {"decision": "accept"},
    }


# --------------------------------------------------------- an MCP elicitation


async def test_an_elicitation_on_a_thread_with_no_terminal_is_declined(harness: Harness) -> None:
    """Nobody could answer it, and a request left open would stop the turn for good."""
    await running_here(harness)
    await harness.daemon.ask(41, ELICITATION_METHOD, elicitation())
    await declined_next(harness, 41)
    assert harness.events("approval") == []


async def test_an_elicitation_is_left_to_a_terminal_in_the_thread(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = watching(harness, monkeypatch)
    await started(harness, loaded=[THREAD])
    await harness.hub.send({"id": "req-1", "session_id": THREAD, "text": "go"})
    assert runner_of(harness).terminal_holds

    await harness.daemon.ask(42, ELICITATION_METHOD, elicitation())
    await settle(lambda: 42 in done)
    harness.terminals.cwds = set()
    await harness.service.refresh_terminals()
    assert not runner_of(harness).terminal_holds

    # Once the terminal has gone the same question is declined, and only that one.
    await harness.daemon.ask(43, ELICITATION_METHOD, elicitation())
    await declined_next(harness, 43)


async def test_nothing_is_answered_in_a_turn_a_terminal_started(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan that finds terminals is blind to some, and an answer would be for them."""
    done = watching(harness, monkeypatch)
    await device_driven(harness)
    await harness.daemon.notify("turn/started", {"threadId": THREAD, "turn": {"id": "turn-7"}})
    runner = runner_of(harness)
    await settle(lambda: runner.busy)
    assert not runner.local_turn and not runner.terminal_holds

    await harness.daemon.ask(51, ELICITATION_METHOD, elicitation())
    await harness.daemon.ask(53, UNHANDLED[0][0], UNHANDLED[0][1])
    await settle(lambda: {51, 53} <= set(done))

    # Only a turn this device started on a thread with no terminal leaves nobody to ask.
    await harness.daemon.notify(
        "turn/completed", {"threadId": THREAD, "turn": {"id": "turn-7", "status": "completed"}}
    )
    await settle(lambda: not runner.busy)
    await harness.hub.send({"id": "req-2", "session_id": THREAD, "text": "go"})
    await harness.daemon.ask(52, ELICITATION_METHOD, elicitation())
    await declined_next(harness, 52)


# --------------------------------------------------------------------- attaching


async def test_a_request_replayed_while_a_thread_is_attached_reaches_its_runner(
    harness: Harness,
) -> None:
    """Codex replays what is pending on a thread right behind the answer to `thread/resume`."""
    harness.daemon.replays.append(
        (
            61,
            "item/commandExecution/requestApproval",
            {
                "threadId": THREAD,
                "turnId": "turn-1",
                "itemId": "exec-1",
                "command": "touch replayed.txt",
                "availableDecisions": ["accept", "cancel"],
            },
        )
    )
    await started(harness, loaded=[THREAD])
    await settle(lambda: harness.events("approval"))
    card = harness.events("approval")[0]
    assert card["status"] == "pending"

    await harness.hub.approve(
        {"session_id": THREAD, "request_id": card["request_id"], "option_id": "allow"}
    )
    assert await next_answer(harness) == {
        "jsonrpc": "2.0",
        "id": 61,
        "result": {"decision": "accept"},
    }


async def test_an_attach_that_fails_leaves_no_runner_behind(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(self: CodexDaemonSession) -> None:
        raise RcError("agent_unavailable", "the daemon went away")

    monkeypatch.setattr(CodexDaemonSession, "start", refuse)
    await started(harness, loaded=[THREAD])
    assert harness.hub.entry(THREAD).runner is None
