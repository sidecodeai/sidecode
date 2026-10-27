"""Amendment A55: renaming a Codex thread through the app-server's `thread/name/set`.

Every route goes through `hub.set_options`, the frame an app sends: the agent got
the call, the reply carries the name it holds, and the session did not move up
the list. The shared daemon is the in-process fake of `tests/fake_codex_daemon.py`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.agents.codex import commands as codex_commands
from rc_client.agents.codex import naming
from rc_client.agents.codex import plugin as codex_plugin
from rc_client.agents.codex import runtime as codex_runtime
from rc_client.agents.codex.adapter import CodexRunner
from rc_client.agents.codex.daemon.session import CodexDaemonSession
from rc_client.agents.codex.errors import CodexError
from rc_client.agents.codex.models import ModelCatalog
from rc_client.agents.codex.rpc import CodexAppServer
from rc_client.agents.registry import DetectContext
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.sessions import titles
from rc_client.sessions.hub import SessionEntry
from tests.rename_helpers import summaries, title_events
from tests.test_codex_daemon_sessions import THREAD, Harness, settle, started, thread_row
from tests.test_codex_daemon_sessions import harness as harness
from tests.test_hub import build_hub

LEDGER = "Ledger migration"
EARLIER = "Typecheck the web app"


async def rename(harness: Harness, title: str) -> dict[str, Any]:
    return await harness.hub.set_options({"session_id": THREAD, "title": title})


async def barrier(harness: Harness) -> None:
    """Wait until everything the daemon sent before this call has been handled.

    The answer to a request comes after every notification the daemon sent
    earlier, so by the time it is read the handlers of those are running.
    """
    client = harness.service.client
    await client.request("thread/loaded/list", {})
    await settle(lambda: not client._tasks)


def lone_thread(harness: Harness, control: str = "none") -> SessionEntry:
    """A Codex session on a hub whose daemon was never reached."""
    session = Session(
        session_id=THREAD,
        device_id="dev-1",
        agent="codex",
        cwd="/repo",
        state="idle",
        control=control,  # type: ignore[arg-type]
    )
    return harness.hub.register_mirrored(session)


class StubServer:
    """Enough of a runner's own app-server for a rename: it answers and is alive."""

    def __init__(self, *, alive: bool = True, error: RcError | None = None) -> None:
        self.alive = alive
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[float] = []

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return {}


def own_server(entry: SessionEntry, server: StubServer) -> CodexRunner:
    """Give the session the runner of a daemon that was not up when it started."""
    runner = CodexRunner(entry.channel, binary="/bin/codex", cwd="/repo", catalog=ModelCatalog())
    runner._server = server  # type: ignore[assignment]
    runner._thread_id = THREAD
    entry.runner = runner
    return runner


# ----------------------------------------------------------- the daemon routes


async def test_a_thread_a_session_holds_is_named_on_the_daemon(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    assert isinstance(entry.runner, CodexDaemonSession)
    entry.session.updated_at = 1_000
    harness.frames.clear()

    result = await rename(harness, "  Ledger\tmigration\n")
    await barrier(harness)

    assert harness.daemon.sent("thread/name/set") == [{"threadId": THREAD, "name": LEDGER}]
    assert result["session"]["title"] == LEDGER
    assert result["session"]["updated_at"] == 1_000
    assert titles.pinned(harness.registry, THREAD)
    # The daemon's own `thread/name/updated` is the second word on it, and changes nothing.
    assert title_events(harness.frames) == [LEDGER]
    assert {summary["updated_at"] for summary in summaries(harness.frames)} == {1_000}


async def test_a_thread_nothing_holds_is_named_on_the_daemon_too(harness: Harness) -> None:
    """The daemon serves a thread it has not loaded straight from its store."""
    await started(harness)
    entry = harness.hub.entry(THREAD)
    assert entry.runner is None
    entry.session.updated_at = 1_000

    result = await rename(harness, LEDGER)

    assert harness.daemon.sent("thread/name/set") == [{"threadId": THREAD, "name": LEDGER}]
    assert result["session"]["title"] == LEDGER
    assert result["session"]["updated_at"] == 1_000
    assert entry.runner is None
    assert harness.daemon.sent("thread/resume") == []


async def test_a_session_started_before_the_daemon_asks_its_own_app_server(
    harness: Harness,
) -> None:
    entry = lone_thread(harness, control="remote")
    server = StubServer()
    own_server(entry, server)
    entry.session.updated_at = 1_000

    result = await rename(harness, LEDGER)

    assert server.calls == [("thread/name/set", {"threadId": THREAD, "name": LEDGER})]
    assert harness.daemon.sent("thread/name/set") == []
    assert (result["session"]["title"], result["session"]["updated_at"]) == (LEDGER, 1_000)


async def test_a_runner_whose_app_server_has_exited_falls_back_to_the_daemon(
    harness: Harness,
) -> None:
    await started(harness)
    entry = harness.hub.entry(THREAD)
    server = StubServer(alive=False)
    own_server(entry, server)

    await rename(harness, LEDGER)

    assert server.calls == []
    assert harness.daemon.sent("thread/name/set") == [{"threadId": THREAD, "name": LEDGER}]


# --------------------------------------------------------------------- refusals


async def test_an_archived_thread_cannot_be_renamed(harness: Harness) -> None:
    await started(harness)
    harness.daemon.unnameable.add(THREAD)
    entry = harness.hub.entry(THREAD)
    harness.frames.clear()

    with pytest.raises(RcError) as caught:
        await rename(harness, LEDGER)

    assert (caught.value.code, caught.value.message) == (
        "unsupported",
        "the codex thread is archived or no longer exists",
    )
    assert entry.session.title == EARLIER
    assert not titles.pinned(harness.registry, THREAD)
    assert title_events(harness.frames) == []


async def test_a_codex_that_has_no_such_method_cannot_rename(harness: Harness) -> None:
    await started(harness)
    harness.daemon.errors["thread/name/set"] = "method not found"
    harness.daemon.error_codes["thread/name/set"] = -32601

    with pytest.raises(RcError) as caught:
        await rename(harness, LEDGER)

    assert (caught.value.code, caught.value.message) == (
        "unsupported",
        "this codex version cannot rename a thread",
    )


async def test_a_failure_inside_codex_is_said_in_our_words_not_its_own(
    harness: Harness,
) -> None:
    """Its message can name a path of the machine, and a reply leaves it."""
    await started(harness)
    harness.daemon.errors["thread/name/set"] = (
        "failed to set thread name: io error at /Users/me/.codex/session_index.jsonl"
    )
    harness.daemon.error_codes["thread/name/set"] = -32603

    with pytest.raises(RcError) as caught:
        await rename(harness, LEDGER)

    assert (caught.value.code, caught.value.message) == (
        "agent_unavailable",
        "codex could not rename the thread",
    )
    assert not titles.pinned(harness.registry, THREAD)


async def test_a_daemon_that_never_answers_times_out(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is `naming.TIMEOUT`, so a short one stands in for the real one here."""
    await started(harness)
    monkeypatch.setattr(naming, "TIMEOUT", 0.05)
    harness.daemon.silent.add("thread/name/set")

    with pytest.raises(RcError) as caught:
        await rename(harness, LEDGER)

    assert (caught.value.code, caught.value.message) == (
        "timeout",
        "codex thread/name/set timed out",
    )
    assert not titles.pinned(harness.registry, THREAD)


async def test_the_rename_waits_fifteen_seconds_on_the_daemon_connection(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production value, read off the request as it is sent, not one a test chose."""
    await started(harness, loaded=[THREAD])
    client = harness.service.client
    asking = client.request
    waited: list[float] = []

    async def watching(method: str, params: dict[str, Any], timeout: float = 60.0) -> Any:
        if method == "thread/name/set":
            waited.append(timeout)
        return await asking(method, params, timeout=timeout)

    monkeypatch.setattr(client, "request", watching)

    await rename(harness, LEDGER)

    assert waited == [15.0]
    assert naming.TIMEOUT == 15.0


async def test_the_rename_waits_fifteen_seconds_on_a_runners_own_connection(
    harness: Harness,
) -> None:
    entry = lone_thread(harness, control="remote")
    server = StubServer()
    own_server(entry, server)

    await rename(harness, LEDGER)

    assert server.timeouts == [15.0]


async def test_no_daemon_and_no_runner_leaves_the_rename_to_the_terminal(
    harness: Harness,
) -> None:
    entry = lone_thread(harness)

    with pytest.raises(RcError) as caught:
        await rename(harness, LEDGER)

    assert (caught.value.code, caught.value.message) == (
        "unsupported",
        "the codex daemon is not connected; run rc-client codex setup",
    )
    assert entry.session.title == ""
    assert not titles.pinned(harness.registry, THREAD)


async def test_a_machine_without_codex_says_so(tmp_path: Path) -> None:
    hub, _, registry = build_hub(tmp_path)
    session = Session(session_id=THREAD, device_id="dev-1", agent="codex", cwd="/repo")
    hub.register_mirrored(session)

    with pytest.raises(RcError) as caught:
        await hub.set_options({"session_id": THREAD, "title": LEDGER})

    assert (caught.value.code, caught.value.message) == (
        "agent_unavailable",
        "codex is not installed on this device",
    )
    registry.close()


async def test_the_app_servers_own_refusal_keeps_its_code_for_the_rename() -> None:
    """Both transports carry the JSON-RPC code, which is what tells a thread Codex cannot name."""
    loop = asyncio.get_running_loop()
    server = CodexAppServer("/bin/codex")
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    server._pending[7] = future
    server._handle({"id": 7, "error": {"code": -32600, "message": "thread not found: x"}})
    with pytest.raises(CodexError) as caught:
        await future
    assert (caught.value.code, caught.value.rpc_code) == ("agent_unavailable", -32600)

    odd: asyncio.Future[dict[str, Any]] = loop.create_future()
    server._pending[8] = odd
    server._handle({"id": 8, "error": {"code": "boom", "message": "no code"}})
    with pytest.raises(CodexError) as caught:
        await odd
    assert caught.value.rpc_code is None


async def test_a_runner_with_its_own_app_server_refuses_in_the_same_words(
    harness: Harness,
) -> None:
    entry = lone_thread(harness, control="remote")
    own_server(entry, StubServer(error=CodexError("thread not found: x", -32600)))

    with pytest.raises(RcError) as caught:
        await rename(harness, LEDGER)

    assert (caught.value.code, caught.value.message) == (
        "unsupported",
        "the codex thread is archived or no longer exists",
    )


# ------------------------------------------------------ names the daemon reports


async def test_a_name_the_daemon_announces_replaces_a_name_an_app_gave(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    entry.session.updated_at = 1_000
    await rename(harness, LEDGER)

    await harness.daemon.notify(
        "thread/name/updated", {"threadId": THREAD, "threadName": "Fix the web typecheck"}
    )
    await barrier(harness)

    assert entry.session.title == "Fix the web typecheck"
    assert entry.session.updated_at == 1_000


async def test_a_name_set_where_the_daemon_is_not_told_is_followed_from_the_index(
    harness: Harness,
) -> None:
    """A TUI with an app-server of its own persists the name and tells nobody."""
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    await rename(harness, LEDGER)
    await barrier(harness)
    entry.session.updated_at = 1_000
    harness.frames.clear()

    # The row is what the next `thread/list` says; no notification came.
    harness.daemon.replies["thread/list"]["data"][0]["name"] = "Fix the web typecheck"
    await harness.service.refresh()

    assert entry.session.title == "Fix the web typecheck"
    assert entry.session.updated_at == 1_000
    assert title_events(harness.frames) == ["Fix the web typecheck"]


async def test_an_index_answer_older_than_our_rename_does_not_put_the_old_name_back(
    harness: Harness,
) -> None:
    """The list is asked for, then the rename lands, then the answer is read."""
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    row = harness.daemon.replies["thread/list"]["data"][0]
    row["name"] = "Earlier name"
    await harness.service.refresh()
    assert entry.session.title == "Earlier name"

    await rename(harness, LEDGER)
    await barrier(harness)
    assert entry.session.title == LEDGER

    row["name"] = "Earlier name"  # the answer, as it was before the rename
    await harness.service.refresh()
    assert entry.session.title == LEDGER

    row["name"] = LEDGER  # and the next one, which has caught up
    await harness.service.refresh()
    assert entry.session.title == LEDGER


async def test_an_index_answer_older_than_a_name_announced_does_not_revert_it(
    harness: Harness,
) -> None:
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    row = harness.daemon.replies["thread/list"]["data"][0]
    row["name"] = "Earlier name"
    await harness.service.refresh()

    await harness.daemon.notify(
        "thread/name/updated", {"threadId": THREAD, "threadName": "Fix the web typecheck"}
    )
    await barrier(harness)
    assert entry.session.title == "Fix the web typecheck"

    await harness.service.refresh()  # the row still says "Earlier name"

    assert entry.session.title == "Fix the web typecheck"


async def test_an_unchanged_index_touches_nothing(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    entry = harness.hub.entry(THREAD)
    harness.daemon.replies["thread/list"]["data"][0]["name"] = LEDGER
    await harness.service.refresh()
    assert entry.session.title == LEDGER
    harness.frames.clear()

    await harness.service.refresh()
    await harness.service.refresh()

    assert harness.frames == []


async def test_a_thread_the_index_names_is_the_users_from_the_first_sight(
    harness: Harness,
) -> None:
    harness.daemon.replies["thread/list"] = {"data": [thread_row(name=LEDGER)]}
    harness.daemon.replies["thread/loaded/list"] = {"data": []}
    assert await harness.service.start("/bin/codex") is True

    assert harness.hub.entry(THREAD).session.title == LEDGER
    assert titles.pinned(harness.registry, THREAD)


async def test_a_thread_without_a_name_keeps_the_title_an_app_gave_it(harness: Harness) -> None:
    await started(harness)
    entry = harness.hub.entry(THREAD)
    await titles.from_user(entry.channel, "Chosen at the start")

    await harness.service.refresh()

    assert entry.session.title == "Chosen at the start"


async def test_a_forgotten_thread_is_followed_afresh_when_it_returns(harness: Harness) -> None:
    await started(harness, loaded=[THREAD])
    row = harness.daemon.replies["thread/list"]["data"][0]
    row["name"] = LEDGER
    await harness.service.refresh()
    assert harness.service._indexed == {THREAD: LEDGER}

    await harness.daemon.notify("thread/archived", {"threadId": THREAD})
    await barrier(harness)

    assert harness.service._indexed == {}


async def test_detection_lists_rename_last_and_the_plugin_has_the_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With a binary found, detection runs `codex --version` and starts a throwaway
    # app-server to ask for its models, so none may be found.
    monkeypatch.setattr(codex_runtime, "resolve_binary", lambda: None)

    info = await codex_plugin.detect(DetectContext(codex_daemon_ready=False))

    assert info.capabilities[-1] == "rename"
    assert callable(getattr(registry.plugin("codex"), "rename", None))


async def test_naming_a_thread_is_a_setting_and_never_a_command(harness: Harness) -> None:
    """A55: `/rename` is `session.set`, so no session offers it or runs it."""
    assert {"rename", "name"}.isdisjoint(command.name for command in codex_commands.listing())
    entry = lone_thread(harness, control="remote")
    runner = own_server(entry, server := StubServer())

    with pytest.raises(RcError) as caught:
        await runner.command("rename", LEDGER, "b1")

    assert caught.value.code == "not_found"
    assert server.calls == []
